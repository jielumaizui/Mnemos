"""
SignalStore (Psyche) 单元测试

覆盖公共行为：
1. SignalStore.__init__() — 初始化（含数据库创建）
2. insert_session_signal() — session 信号存储
3. insert_git_signal() — git 信号存储
4. insert_note_signal() — notes 信号存储
5. get_signal_stats() — 聚合统计
6. get_signal_health() — 健康度评估
7. get_unprocessed_signals() + mark_signals_processed() — 未处理信号流
8. save_persona_version() + get_latest_persona_version() — 画像版本管理
9. handle_event() — 事件式信号注入
10. get_signal_store() — 单例行为
11. 持久化 — 信号在重新实例化后仍然可读
12. 去重 — 重复插入返回已有 id
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from core.persona.psyche import (
    GitSignal,
    NoteSignal,
    SessionSignal,
    SignalStore,
    WechatSignal,
    get_signal_store,
)
from tests.persona_decision_fixtures import (
    save_persona_version_authorized,
    update_blindspot_profile_authorized,
)

# ---------- Fixture ----------


@pytest.fixture(autouse=True)  # noqa
def reset_signal_store_singleton():
    """每个测试前后清理 SignalStore 单例，防止状态泄漏。"""
    global _signal_store
    _signal_store = None
    yield
    _signal_store = None


@pytest.fixture
def store(tmp_path):
    """返回一个使用临时数据库的 SignalStore 实例。"""
    db = tmp_path / "test_signals.db"
    s = SignalStore(initialize_schema=True, db_path=db)
    yield s
    s.close()


# ---------- 初始化 ----------


def test_init_creates_database_and_tables(tmp_path):
    """初始化时应自动创建数据库文件和所有表结构。"""
    db = tmp_path / "new.db"
    assert not db.exists()

    s = SignalStore(initialize_schema=True, db_path=db)
    assert db.exists()

    # 验证核心表已创建
    conn = s._pool.get_conn()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cursor.fetchall()}
    expected = {
        "session_signals",
        "knowledge_signals",
        "git_signals",
        "file_system_signals",
        "signal_metadata",
        "signal_daily_index",
        "persona_versions",
        "note_signals",
        "document_signals",
        "reflection_signals",
    }
    assert expected.issubset(tables)
    s.close()


def test_signal_store_uses_short_sqlite_timeout(tmp_path, monkeypatch):
    """画像信号库默认连接预算应短，避免 MCP 高频入口被 SQLite 锁拖慢。"""
    captured = {}

    class FakePool:
        def __init__(self, db_path, **kwargs):
            captured["db_path"] = db_path
            captured.update(kwargs)

        def close(self):
            return None

    monkeypatch.setattr("core.db_utils.SqlitePool", FakePool)
    monkeypatch.setattr(SignalStore, "_init_db", lambda self: None)

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "signals.db")

    assert captured["timeout"] == 2.0
    assert captured["busy_timeout_ms"] == 2000
    assert captured["persistent"] is False
    store.close()


def test_signal_store_releases_nonpersistent_connections_after_public_calls(store):
    sig = SessionSignal(
        session_id="release-s1",
        timestamp=datetime.now().isoformat(),
        task_type="coding",
    )

    store.insert_session_signal(sig)
    assert store._pool._transient_conns == set()

    stats = store.get_signal_stats(days=30)

    assert stats["session"] >= 1
    assert store._pool._transient_conns == set()


def test_add_reflection_signal(store):
    """add_signal 应写入 reflection_signals 表并返回正整数 id。"""
    sid = store.add_signal(
        dimension="reflection_interest",
        value="growth",
        confidence=0.8,
        source="layer5_insight",
    )
    assert isinstance(sid, int)
    assert sid > 0

    conn = store._pool.get_conn()
    conn.row_factory = sqlite3.Row  # noqa
    row = conn.execute(
        "SELECT dimension, value, confidence, source FROM reflection_signals WHERE id = ?",
        (sid,),
    ).fetchone()
    assert row is not None
    assert row["dimension"] == "reflection_interest"
    assert row["value"] == "growth"
    assert row["confidence"] == pytest.approx(0.8)
    assert row["source"] == "layer5_insight"


# ---------- Session 信号 ----------


def test_insert_session_signal_returns_positive_id(store):
    """插入 session 信号应返回正整数 id。"""
    sig = SessionSignal(
        session_id="sess-001",
        timestamp=datetime.now().isoformat(),
        task_type="coding",
        user_msg_count=5,
        avg_user_msg_length=128.5,
        provided_context_richness=0.75,
        correction_domains=["requirements", "scope"],
        options_presented=3,
        option_selected=2,
        selection_rationale="用户选择了第二个方案，因为它更接近当前验收范围",
        final_feedback="需要更明确的验收标准",
        output_type="code",
        output_file_count=4,
        duration_seconds=300,
    )
    assert sig.provided_context_richness == pytest.approx(0.75)
    assert sig.options_presented == 3
    assert sig.option_selected == 2
    assert sig.output_file_count == 4
    assert asdict(sig)["selection_rationale"] == "用户选择了第二个方案，因为它更接近当前验收范围"
    assert asdict(sig)["final_feedback"] == "需要更明确的验收标准"

    sid = store.insert_session_signal(sig)
    assert isinstance(sid, int)
    assert sid > 0
    row = (
        store._pool.get_conn()
        .execute(
            "SELECT avg_user_msg_length, provided_context_richness, "
            "correction_domains, options_presented, option_selected, "
            "selection_rationale, final_feedback, output_type, output_file_count "
            "FROM session_signals WHERE id = ?",
            (sid,),
        )
        .fetchone()
    )
    assert row[0] == 128.5
    assert row[1] == pytest.approx(0.75)
    assert json.loads(row[2]) == ["requirements", "scope"]
    assert row[3] == 3
    assert row[4] == 2
    assert row[5] == "用户选择了第二个方案，因为它更接近当前验收范围"
    assert row[6] == "需要更明确的验收标准"
    assert row[7] == "code"
    assert row[8] == 4


def test_project_isolated_signals_escape_like_wildcards(store):
    """项目隔离查询应把项目路径当字面值，而不是 SQL LIKE 模式。"""
    timestamp = datetime.now().isoformat()
    literal_project = "/repo/mnemos%core"
    wildcard_project = "/repo/mnemosXcore"

    store.insert_session_signal(
        SessionSignal(
            session_id="literal-session", timestamp=timestamp, working_dir=literal_project
        )
    )
    store.insert_session_signal(
        SessionSignal(session_id="wild-session", timestamp=timestamp, working_dir=wildcard_project)
    )
    store.insert_git_signal(
        GitSignal(repo_path=literal_project, commit_hash="literal-git", timestamp=timestamp)
    )
    store.insert_git_signal(
        GitSignal(repo_path=wildcard_project, commit_hash="wild-git", timestamp=timestamp)
    )
    store.insert_file_system_signal(
        f"{literal_project}/README.md",
        "modify",
        timestamp,
        project_name="mnemos%core",
    )
    store.insert_file_system_signal(
        f"{wildcard_project}/README.md",
        "modify",
        timestamp,
        project_name="mnemosXcore",
    )

    signals = store.get_project_isolated_signals(literal_project, days=1)

    assert {row["session_id"] for row in signals["session"]} == {"literal-session"}
    assert {row["commit_hash"] for row in signals["git"]} == {"literal-git"}
    assert {row["file_path"] for row in signals["file_system"]} == {f"{literal_project}/README.md"}


def test_get_signal_projects_lists_non_empty_recent_projects(store):
    """项目信号列表应只返回最近窗口内的真实项目路径。"""
    now = datetime.now()
    recent = now.isoformat()
    old = (now - timedelta(days=60)).isoformat()

    store.insert_session_signal(
        SessionSignal(session_id="recent-session-1", timestamp=recent, working_dir="/repo/mnemos")
    )
    store.insert_session_signal(
        SessionSignal(session_id="recent-session-2", timestamp=recent, working_dir="/repo/mnemos")
    )
    store.insert_session_signal(
        SessionSignal(session_id="blank-session", timestamp=recent, working_dir="")
    )
    store.insert_session_signal(
        SessionSignal(session_id="old-session", timestamp=old, working_dir="/repo/old")
    )
    store.insert_git_signal(
        GitSignal(repo_path="/repo/mnemos", commit_hash="recent-git", timestamp=recent)
    )
    store.insert_git_signal(GitSignal(repo_path="", commit_hash="blank-git", timestamp=recent))
    store.insert_git_signal(GitSignal(repo_path="/repo/old", commit_hash="old-git", timestamp=old))

    projects = store.get_signal_projects(days=30)

    assert projects == [
        {"type": "session", "identifier": "/repo/mnemos", "signal_count": 2},
        {"type": "git", "identifier": "/repo/mnemos", "signal_count": 1},
    ]


def test_insert_session_signal_with_metadata(store):
    """插入 session 信号时应同时写入 signal_metadata。"""
    sig = SessionSignal(
        session_id="sess-002",
        timestamp=datetime.now().isoformat(),
        task_type="debugging",
    )
    ctx = {"working_dir": "/tmp/proj", "agent": "claude"}
    sid = store.insert_session_signal(sig, session_context=ctx)

    # 验证 metadata 表中存在对应记录
    conn = store._pool.get_conn()
    row = conn.execute(
        "SELECT confidence, processed, session_context FROM signal_metadata "
        "WHERE signal_table = ? AND signal_id = ?",
        ("session", sid),
    ).fetchone()
    assert row is not None
    assert row[0] == 1.0
    assert row[1] == 0
    assert json.loads(row[2]) == ctx


def test_session_exists_after_insert(store):
    """插入后 session_exists 应返回 True。"""
    sig = SessionSignal(
        session_id="sess-exists",
        timestamp=datetime.now().isoformat(),
    )
    store.insert_session_signal(sig)
    assert store.session_exists("sess-exists") is True
    assert store.session_exists("sess-not-exists") is False


# ---------- Git 信号 ----------


def test_insert_git_signal_and_metadata(store):
    """插入 git 信号应返回正 id，并在 metadata 中标记较低置信度。"""
    sig = GitSignal(
        repo_path="/tmp/repo",
        commit_hash="abc123",
        timestamp=datetime.now().isoformat(),
        message_length=42,
        has_issue_reference=True,
        has_pr_reference=True,
        commit_type="feat",
        is_weekend=True,
    )
    gid = store.insert_git_signal(sig)
    assert gid > 0
    assert sig.is_weekend is True

    conn = store._pool.get_conn()
    git_row = conn.execute(
        "SELECT message_length, has_issue_reference, has_pr_reference, is_weekend "
        "FROM git_signals WHERE id = ?",
        (gid,),
    ).fetchone()
    assert git_row[0] == 42
    assert git_row[1] == 1
    assert git_row[2] == 1
    assert git_row[3] == 1

    row = conn.execute(
        "SELECT confidence, possible_external_factors FROM signal_metadata "
        "WHERE signal_table = ? AND signal_id = ?",
        ("git", gid),
    ).fetchone()
    assert row[0] == 0.7
    assert "possible_company_policy" in json.loads(row[1])


def test_insert_git_signal_persists_change_counts(store):
    """Git 文件改动统计字段应进入 asdict 和 git_signals 表。"""
    sig = GitSignal(
        repo_path="/tmp/repo",
        commit_hash="stats123",
        timestamp=datetime.now().isoformat(),
        files_changed=6,
        lines_added=120,
        lines_deleted=18,
        test_files_changed=2,
    )

    assert asdict(sig)["test_files_changed"] == 2

    gid = store.insert_git_signal(sig)
    row = (
        store._pool.get_conn()
        .execute(
            "SELECT files_changed, lines_added, lines_deleted, test_files_changed "
            "FROM git_signals WHERE id = ?",
            (gid,),
        )
        .fetchone()
    )

    assert row == (6, 120, 18, 2)


def test_git_commit_exists_after_insert(store):
    """插入后 git_commit_exists 应返回 True。"""
    sig = GitSignal(
        repo_path="/tmp/repo",
        commit_hash="def456",
        timestamp=datetime.now().isoformat(),
    )
    store.insert_git_signal(sig)
    assert store.git_commit_exists("def456") is True
    assert store.git_commit_exists("no-such-hash") is False


# ---------- Note 信号 ----------


def test_insert_note_signal_and_metadata(store):
    """插入 notes 信号应返回正 id；AI 生成内容置信度应更低。"""
    sig_human = NoteSignal(
        timestamp=datetime.now().isoformat(),
        content_length=200,
        has_title=True,
        has_link=True,
        image_count=2,
        tag_count=3,
        is_ai_generated=False,
        note_uid="memo-human",
    )
    sid_human = store.insert_note_signal(sig_human)
    assert sid_human > 0

    sig_ai = NoteSignal(
        timestamp=datetime.now().isoformat(),
        content_length=100,
        is_ai_generated=True,
        ai_agent="claude",
        note_uid="memo-ai",
    )
    sid_ai = store.insert_note_signal(sig_ai)
    assert sid_ai > 0

    conn = store._pool.get_conn()
    conf_human = conn.execute(
        "SELECT confidence FROM signal_metadata WHERE signal_table = ? AND signal_id = ?",
        ("notes", sid_human),
    ).fetchone()[0]
    conf_ai = conn.execute(
        "SELECT confidence FROM signal_metadata WHERE signal_table = ? AND signal_id = ?",
        ("notes", sid_ai),
    ).fetchone()[0]
    ai_agent = conn.execute("SELECT ai_agent FROM note_signals WHERE id = ?", (sid_ai,)).fetchone()[
        0
    ]
    content_length = conn.execute(
        "SELECT content_length FROM note_signals WHERE id = ?", (sid_human,)
    ).fetchone()[0]
    note_meta = conn.execute(
        "SELECT has_title, has_link, image_count, tag_count FROM note_signals WHERE id = ?",
        (sid_human,),
    ).fetchone()
    assert conf_human == 0.8
    assert conf_ai == 0.5
    assert content_length == 200
    assert tuple(note_meta) == (1, 1, 2, 3)
    assert ai_agent == "claude"


def test_wechat_signal_chat_type_serialized_contract():
    """WechatSignal.chat_type 是微信聊天信号序列化契约字段。"""
    signal = WechatSignal(
        timestamp=datetime.now().isoformat(),
        content_hash="wx-chat-1",
        chat_type="group",
    )

    assert signal.chat_type == "group"
    assert asdict(signal)["chat_type"] == "group"


def test_wechat_signal_emotional_arousal_persisted_contract(store):
    """WechatSignal.emotional_arousal 是微信情绪强度入库契约字段。"""
    signal = WechatSignal(
        timestamp=datetime.now().isoformat(),
        content_hash="wx-arousal-1",
        msg_length=88,
        has_sensitive_content=True,
        emotional_arousal=0.72,
        emotional_valence=-0.25,
        topic_tags=["deadline", "stress"],
        msg_sequence_in_day=5,
    )

    signal_id = store.insert_wechat_signal(signal)

    conn = store._pool.get_conn()
    row = conn.execute(
        "SELECT msg_length, has_sensitive_content, emotional_arousal, emotional_valence, "
        "topic_tags, msg_sequence_in_day FROM wechat_signals WHERE id = ?",
        (signal_id,),
    ).fetchone()
    assert asdict(signal)["emotional_arousal"] == 0.72
    assert row[0] == 88
    assert row[1] == 1
    assert row[2] == 0.72
    assert row[3] == -0.25
    assert json.loads(row[4]) == ["deadline", "stress"]
    assert row[5] == 5


def test_wechat_signal_emotional_valence_persisted_contract(store):
    """WechatSignal.emotional_valence 是微信情绪效价入库契约字段。"""
    signal = WechatSignal(
        timestamp=datetime.now().isoformat(),
        content_hash="wx-valence-1",
        emotional_valence=0.34,
    )

    signal_id = store.insert_wechat_signal(signal)

    conn = store._pool.get_conn()
    row = conn.execute(
        "SELECT emotional_valence FROM wechat_signals WHERE id = ?",
        (signal_id,),
    ).fetchone()
    assert asdict(signal)["emotional_valence"] == 0.34
    assert row[0] == 0.34


def test_get_recent_note_and_wechat_signals_filter_by_days(store):
    """最近 notes/wechat 读取 API 应按天数返回结构化字典行。"""
    old_timestamp = (datetime.now() - timedelta(days=40)).isoformat()
    recent_timestamp = (datetime.now() - timedelta(days=3)).isoformat()

    store.insert_note_signal(
        NoteSignal(timestamp=old_timestamp, note_uid="old-note", content_length=10)
    )
    store.insert_note_signal(
        NoteSignal(timestamp=recent_timestamp, note_uid="recent-note", content_length=20)
    )
    store.insert_wechat_signal(
        WechatSignal(timestamp=old_timestamp, content_hash="old-wx", msg_length=5)
    )
    store.insert_wechat_signal(
        WechatSignal(timestamp=recent_timestamp, content_hash="recent-wx", msg_length=15)
    )

    note_rows = store.get_recent_note_signals(days=7)
    wechat_rows = store.get_recent_wechat_signals(days=7)

    assert [row["note_uid"] for row in note_rows] == ["recent-note"]
    assert [row["content_hash"] for row in wechat_rows] == ["recent-wx"]
    assert note_rows[0]["content_length"] == 20
    assert wechat_rows[0]["msg_length"] == 15


# ---------- 统计与健康度 ----------


def test_get_signal_stats_counts_by_source(store):
    """get_signal_stats 应正确统计各来源信号数量。"""
    # 插入 session 信号
    store.insert_session_signal(
        SessionSignal(
            session_id="s1",
            timestamp=datetime.now().isoformat(),
        )
    )
    # 插入 knowledge 信号
    store.insert_knowledge_signal("page.md", "access", datetime.now().isoformat())
    # 插入 notes 信号
    store.insert_note_signal(NoteSignal(timestamp=datetime.now().isoformat(), note_uid="m1"))

    stats = store.get_signal_stats(days=30)
    assert stats["session"] == 1
    assert stats["knowledge"] == 1
    assert stats["notes"] == 1
    assert stats["git"] == 0
    assert stats["file_system"] == 0


def test_get_daily_summary_computes_from_signal_tables(store):
    """get_daily_summary 在索引为空时应从信号表实时聚合。"""
    date = "2026-07-01"
    store.insert_session_signal(
        SessionSignal(
            session_id="daily-session",
            timestamp=f"{date}T10:00:00",
            task_type="coding",
            agent="codex",
        )
    )
    store.insert_git_signal(
        GitSignal(
            repo_path="/tmp/repo",
            commit_hash="daily-git",
            timestamp=f"{date}T11:00:00",
            commit_type="feat",
        )
    )
    store.insert_note_signal(
        NoteSignal(
            note_uid="daily-note",
            timestamp=f"{date}T12:00:00",
            is_ai_generated=True,
            ai_agent="codex",
        )
    )
    store.insert_knowledge_signal("daily.md", "access", f"{date}T13:00:00")

    summary = store.get_daily_summary(date)

    assert summary["session"]["signal_count"] == 1
    assert summary["session"]["summary"]["task_type"] == {"coding": 1}
    assert summary["session"]["summary"]["agent"] == {"codex": 1}
    assert summary["git"]["summary"]["commit_type"] == {"feat": 1}
    assert summary["notes"]["summary"]["is_ai_generated"] == {"1": 1}
    assert summary["notes"]["summary"]["ai_agent"] == {"codex": 1}
    assert summary["knowledge"]["summary"]["action_type"] == {"access": 1}


def test_get_daily_summary_reads_existing_index(store):
    """已有 signal_daily_index 时应优先返回索引摘要。"""
    conn = store._pool.get_conn()
    conn.execute(
        """
        INSERT INTO signal_daily_index (date, source_type, signal_count, summary_json)
        VALUES (?, ?, ?, ?)
        """,
        ("2026-07-01", "session", 3, '{"task_type": {"coding": 3}}'),
    )
    conn.commit()

    summary = store.get_daily_summary("2026-07-01")

    assert summary["session"]["signal_count"] == 3
    assert summary["session"]["summary"] == {"task_type": {"coding": 3}}


def test_get_signal_health_structure(store):
    """get_signal_health 应返回包含四类核心信号的结构，fs 标记为 optional。"""
    health = store.get_signal_health(days=30)
    assert set(health.keys()) == {"session", "git", "wiki", "notes", "file_system"}
    assert health["file_system"]["optional"] is True
    assert health["file_system"]["healthy"] is True
    for key in ("session", "git", "wiki", "notes"):
        assert "count" in health[key]
        assert "min" in health[key]
        assert "weight" in health[key]
        assert "healthy" in health[key]


# ---------- 未处理信号流 ----------


def test_unprocessed_signals_and_mark_processed(store):
    """未处理信号应能被查询并标记为已处理。"""
    sig = SessionSignal(
        session_id="s-unproc",
        timestamp=datetime.now().isoformat(),
    )
    sid = store.insert_session_signal(sig)

    unproc = store.get_unprocessed_signals("session", limit=10)
    assert len(unproc) == 1
    assert unproc[0]["session_id"] == "s-unproc"

    store.mark_signals_processed("session", [sid])
    unproc_after = store.get_unprocessed_signals("session", limit=10)
    assert len(unproc_after) == 0


# ---------- 画像版本管理 ----------


def test_save_and_get_latest_persona_version(store):
    """保存画像版本后应能通过 get_latest_persona_version 读取。"""
    save_persona_version_authorized(
        store,
        version=1,
        period_start="2024-01-01",
        period_end="2024-01-31",
        energy={"focus": 0.8},
        cognitive={"deduction": 0.7},
        value={"depth_vs_breadth": 0.9},
        blindspot={"gaps": ["architecture"]},
        signal_count=42,
    )

    latest = store.get_latest_persona_version()
    assert latest is not None
    assert latest["version"] == 1
    assert latest["signal_count_used"] == 42
    assert latest["energy_profile"] == {"focus": 0.8}
    assert latest["cognitive_profile"] == {"deduction": 0.7}
    assert latest["value_profile"] == {"depth_vs_breadth": 0.9}
    assert latest["blindspot_profile"] == {"gaps": ["architecture"]}


def test_persona_revision_commits_exact_signal_cursor_atomically(store, monkeypatch):
    """A failed revision cannot consume its source signals without the revision."""

    import core.persona.psyche_persona as psyche_module

    signal_id = store.insert_session_signal(
        SessionSignal(session_id="cursor-atomic", timestamp=datetime.now().isoformat())
    )
    kwargs = {
        "version": 1,
        "period_start": "2026-07-01",
        "period_end": "2026-07-17",
        "energy": {"focus": 0.8},
        "cognitive": {"deduction": 0.9},
        "value": {"depth": 0.9},
        "blindspot": {},
        "signal_count": 1,
        "source_signal_ids": {"session": [signal_id]},
    }
    original = psyche_module.record_target_effect

    def fail_before_commit(*_args, **_kwargs):
        raise OSError("forced target receipt failure")

    monkeypatch.setattr(psyche_module, "record_target_effect", fail_before_commit)
    with pytest.raises(OSError, match="forced target receipt failure"):
        save_persona_version_authorized(store, **kwargs)

    conn = store._pool.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM persona_revisions").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT processed FROM signal_metadata WHERE signal_table='session' AND signal_id=?",
            (signal_id,),
        ).fetchone()[0]
        == 0
    )

    monkeypatch.setattr(psyche_module, "record_target_effect", original)
    save_persona_version_authorized(store, **kwargs)

    conn = store._pool.get_conn()
    row = conn.execute("SELECT source_cursor FROM persona_revisions WHERE version=1").fetchone()
    assert json.loads(row[0])["source_signal_ids"] == {"session": [signal_id]}
    assert (
        conn.execute(
            "SELECT processed FROM signal_metadata WHERE signal_table='session' AND signal_id=?",
            (signal_id,),
        ).fetchone()[0]
        == 1
    )


def test_persona_signal_cursor_accumulates_pending_submaterial_evidence(store):
    """Uncommitted batches stay durable and a later revision consumes their union."""

    first_id = store.insert_session_signal(
        SessionSignal(session_id="submaterial-first", timestamp=datetime.now().isoformat())
    )
    assert [row["id"] for row in store.get_unprocessed_signals("session")] == [first_id]

    second_id = store.insert_session_signal(
        SessionSignal(session_id="submaterial-second", timestamp=datetime.now().isoformat())
    )
    pending_ids = [row["id"] for row in store.get_unprocessed_signals("session")]
    assert pending_ids == [first_id, second_id]

    save_persona_version_authorized(
        store,
        version=1,
        period_start="2026-07-01",
        period_end="2026-07-17",
        energy={"focus": 0.8},
        cognitive={"deduction": 0.9},
        value={"depth": 0.9},
        blindspot={},
        signal_count=2,
        source_signal_ids={"session": pending_ids},
    )

    assert store.get_unprocessed_signals("session") == []


def test_persona_signal_cursor_rejects_new_command_replaying_consumed_source(store):
    """A new command cannot silently reuse an already committed source cursor."""

    signal_id = store.insert_session_signal(
        SessionSignal(session_id="cursor-replay", timestamp=datetime.now().isoformat())
    )
    kwargs = {
        "version": 1,
        "period_start": "2026-07-01",
        "period_end": "2026-07-17",
        "energy": {"focus": 0.8},
        "cognitive": {"deduction": 0.9},
        "value": {"depth": 0.9},
        "blindspot": {},
        "signal_count": 1,
        "source_signal_ids": {"session": [signal_id]},
    }

    first_row_id = save_persona_version_authorized(store, **kwargs)
    with pytest.raises(ValueError, match="already consumed"):
        save_persona_version_authorized(store, **kwargs)

    conn = store._pool.get_conn()
    assert first_row_id > 0
    assert conn.execute("SELECT COUNT(*) FROM persona_revisions").fetchone()[0] == 1


def test_persona_version_recovers_target_commit_without_duplicate(
    store,
    monkeypatch,
):
    import core.persona.psyche_persona as psyche_module
    from core.persona.psyche import (
        PERSONA_VERSION_ACTION,
        PERSONA_VERSION_EXECUTOR,
        PERSONA_VERSION_OWNER,
        persona_version_material_action_binding,
    )
    from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal
    from tests.cognitive_decision_fixtures import material_action_authorization

    signal_id = store.insert_session_signal(
        SessionSignal(session_id="cursor-crash-recovery", timestamp=datetime.now().isoformat())
    )

    kwargs = {
        "version": 41,
        "generated_at": "2026-07-17T09:00:00+00:00",
        "period_start": "2026-07-01",
        "period_end": "2026-07-17",
        "energy": {"focus": 0.8},
        "cognitive": {"deduction": 0.9},
        "value": {"depth": 0.9},
        "blindspot": {"gaps": ["crash recovery"]},
        "signal_count": 17,
        "source_signal_ids": {"session": [signal_id]},
    }
    binding = persona_version_material_action_binding(**kwargs)
    authorization = material_action_authorization(
        store.db_path.parent,
        action_type=PERSONA_VERSION_ACTION,
        owner=PERSONA_VERSION_OWNER,
        executor=PERSONA_VERSION_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    original = psyche_module.recover_recorded_target_effect
    crashed = False

    def crash_after_target(auth, oracle):
        nonlocal crashed
        if not crashed and oracle.observe(auth.permit) is not None:
            crashed = True
            raise OSError("crash after persona target commit")
        return original(auth, oracle)

    monkeypatch.setattr(
        psyche_module,
        "recover_recorded_target_effect",
        crash_after_target,
    )
    with pytest.raises(OSError, match="after persona target commit"):
        store.save_persona_version(**kwargs, material_action=authorization)

    monkeypatch.setattr(
        psyche_module,
        "recover_recorded_target_effect",
        original,
    )
    row_id = store.save_persona_version(**kwargs, material_action=authorization)

    conn = store._pool.get_conn()
    assert (
        conn.execute("SELECT COUNT(*) FROM persona_revisions WHERE version=41").fetchone()[0] == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM material_target_effects").fetchone()[0] == 1
    events = FormalCognitiveMutationJournal.for_database(store.db_path).list_events(
        asset_kind="persona_profile"
    )
    assert row_id > 0
    assert len(events) == 1
    conn = store._pool.get_conn()
    assert (
        conn.execute(
            "SELECT processed FROM signal_metadata WHERE signal_table='session' AND signal_id=?",
            (signal_id,),
        ).fetchone()[0]
        == 1
    )


def test_persona_version_rejects_reused_version_with_different_content(store):
    """A semantic Persona version cannot silently fork into two snapshots."""

    common = {
        "version": 7,
        "period_start": "2026-07-01",
        "period_end": "2026-07-17",
        "cognitive": {"deduction": 0.8},
        "value": {"depth": 0.7},
        "blindspot": {},
        "signal_count": 17,
    }
    save_persona_version_authorized(
        store,
        **common,
        energy={"focus": 0.4},
        generated_at="2026-07-17T09:00:00+00:00",
    )

    with pytest.raises(ValueError, match="already belongs to a different Persona"):
        save_persona_version_authorized(
            store,
            **common,
            energy={"focus": 0.9},
            generated_at="2026-07-18T09:00:00+00:00",
        )

    conn = store._pool.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM persona_revisions WHERE version=7").fetchone()[0] == 1


def test_persona_version_rejects_reused_content_under_another_version(store):
    """A changed version number cannot disguise an already committed Persona body."""

    common = {
        "period_start": "2026-07-01",
        "period_end": "2026-07-17",
        "energy": {"focus": 0.4},
        "cognitive": {"deduction": 0.8},
        "value": {"depth": 0.7},
        "blindspot": {},
        "signal_count": 17,
    }
    save_persona_version_authorized(
        store,
        version=7,
        **common,
        generated_at="2026-07-17T09:00:00+00:00",
    )

    with pytest.raises(ValueError, match="content is already committed"):
        save_persona_version_authorized(
            store,
            version=8,
            **common,
            generated_at="2026-07-18T09:00:00+00:00",
        )


def test_persona_revision_writer_keeps_one_head_under_100_concurrent_commands(store):
    """Concurrent canonical writers cannot fork the immutable Persona chain."""

    from core.persona.psyche import (
        PERSONA_VERSION_ACTION,
        PERSONA_VERSION_EXECUTOR,
        PERSONA_VERSION_OWNER,
        persona_version_material_action_binding,
    )
    from tests.cognitive_decision_fixtures import material_action_authorization

    commands = []
    for version in range(1, 101):
        kwargs = {
            "version": version,
            "generated_at": f"2026-07-17T09:{version % 60:02d}:00+00:00",
            "period_start": "2026-07-01",
            "period_end": "2026-07-17",
            "energy": {"focus": version / 100},
            "cognitive": {"deduction": version / 100},
            "value": {"depth": version / 100},
            "blindspot": {"ordinal": version},
            "signal_count": version,
        }
        binding = persona_version_material_action_binding(**kwargs)
        authorization = material_action_authorization(
            store.db_path.parent,
            action_type=PERSONA_VERSION_ACTION,
            owner=PERSONA_VERSION_OWNER,
            executor=PERSONA_VERSION_EXECUTOR,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
        )
        commands.append((kwargs, authorization))

    def persist(command):
        kwargs, authorization = command
        return store.save_persona_version(**kwargs, material_action=authorization)

    with ThreadPoolExecutor(max_workers=16) as executor:
        row_ids = list(executor.map(persist, commands))

    conn = store._pool.get_conn()
    assert len(set(row_ids)) == 100
    assert conn.execute("SELECT COUNT(*) FROM persona_revisions").fetchone()[0] == 100
    assert conn.execute(
        "SELECT COUNT(DISTINCT version), COUNT(DISTINCT content_hash) FROM persona_revisions"
    ).fetchone() == (100, 100)
    assert conn.execute("SELECT COUNT(*) FROM persona_revision_heads").fetchone()[0] == 1
    head_version = conn.execute("""
        SELECT revision.version
        FROM persona_revision_heads AS head
        JOIN persona_revisions AS revision ON revision.revision_id=head.revision_id
        WHERE head.scope_key='global'
        """).fetchone()[0]
    assert store.get_latest_persona_version()["version"] == head_version
    conn = store._pool.get_conn()
    assert conn.execute("""
        SELECT COUNT(*)
        FROM persona_revisions AS revision
        WHERE revision.supersedes_revision_id IS NULL
        """).fetchone()[0] == 1


def test_persona_revision_head_survives_store_restart(store):
    """The current Persona is read from the durable head, not row selection luck."""

    db_path = store.db_path
    for version, focus in ((1, 0.3), (2, 0.8)):
        save_persona_version_authorized(
            store,
            version=version,
            period_start="2026-07-01",
            period_end="2026-07-17",
            energy={"focus": focus},
            cognitive={"deduction": focus},
            value={"depth": focus},
            blindspot={"ordinal": version},
            signal_count=version,
            generated_at=f"2026-07-1{version}T09:00:00+00:00",
        )
    store.close()

    reopened = SignalStore(db_path=db_path)
    try:
        latest = reopened.get_latest_persona_version()
        head = reopened._pool.get_conn().execute("""
            SELECT revision.version
            FROM persona_revision_heads AS head
            JOIN persona_revisions AS revision ON revision.revision_id=head.revision_id
            WHERE head.scope_key='global'
            """).fetchone()
        assert latest is not None
        assert latest["version"] == 2
        assert head == (2,)
    finally:
        reopened.close()


def test_get_recent_persona_versions(store):
    """get_recent_persona_versions 应按 version 降序返回最近 N 个版本。"""
    for v in [1, 2, 3]:
        save_persona_version_authorized(
            store,
            version=v,
            period_start="2024-01-01",
            period_end="2024-01-31",
            energy={"focus": v},
            cognitive={"deduction": v},
            value={"depth_vs_breadth": v},
            blindspot={},
            signal_count=v * 10,
        )

    versions = store.get_recent_persona_versions(limit=2)
    assert len(versions) == 2
    assert [v["version"] for v in versions] == [3, 2]
    assert versions[0]["energy_profile"] == {"focus": 3}
    assert versions[1]["signal_count_used"] == 20


def test_update_blindspot_profile_appends_immutable_successor(store):
    """A blindspot change advances the canonical head without changing history."""
    save_persona_version_authorized(
        store,
        version=1,
        period_start="2024-01-01",
        period_end="2024-01-31",
        energy={},
        cognitive={},
        value={},
        blindspot={"old": True},
        signal_count=10,
    )
    assert (
        update_blindspot_profile_authorized(
            store,
            {"new": True, "gaps": ["test"]},
        )
        is True
    )

    latest = store.get_latest_persona_version()
    assert latest["blindspot_profile"] == {"new": True, "gaps": ["test"]}
    conn = store._pool.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM persona_revisions").fetchone()[0] == 2
    old = conn.execute(
        "SELECT blindspot_profile FROM persona_revisions WHERE version=1"
    ).fetchone()[0]
    assert json.loads(old) == {"old": True}
    event = conn.execute("SELECT event_type, payload_json FROM persona_blindspot_events").fetchone()
    assert event[0] == "applied"
    assert json.loads(event[1])["state"]["blindspot"] == {"new": True, "gaps": ["test"]}

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE persona_revisions SET generated_at='tampered' WHERE version=1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM persona_blindspot_events")


def test_persona_calibration_event_rebuilds_current_state_from_immutable_chain(store):
    save_persona_version_authorized(
        store,
        version=1,
        period_start="2026-07-01",
        period_end="2026-07-17",
        energy={"focus": 0.7},
        cognitive={"deduction": 0.8},
        value={"depth": 0.9},
        blindspot={"gaps": []},
        signal_count=7,
    )
    authorization = store.prepare_persona_calibration_material_action(
        version=1,
        confirmed_at="2026-07-17T10:00:00+00:00",
        calibration_score=4.0,
        calibration_metadata={"ratings": {"focus_depth": 4}},
        source_facts={"ratings": {"focus_depth": 4}},
        evidence_refs=("calibration-rating:focus_depth",),
    )

    assert authorization is not None
    assert (
        store.record_persona_calibration(
            version=1,
            confirmed_at="2026-07-17T10:00:00+00:00",
            calibration_score=4.0,
            calibration_metadata={"ratings": {"focus_depth": 4}},
            material_action=authorization,
        )
        is True
    )

    rebuilt = store.rebuild_current_persona_state()
    latest = store.get_latest_persona_version()
    assert rebuilt["status"] == "verified"
    assert rebuilt["revision_id"] == latest["revision_id"]
    assert rebuilt["state"]["calibration_score"] == 4.0
    assert rebuilt["state_hash"]

    conn = store._pool.get_conn()
    event = conn.execute(
        "SELECT event_type, payload_json FROM persona_calibration_events"
    ).fetchone()
    assert event[0] == "applied"
    event_payload = json.loads(event[1])
    assert event_payload["state"]["calibration_score"] == 4.0
    assert event_payload["revision_metadata"]["calibration"] == {"ratings": {"focus_depth": 4}}
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE persona_calibration_events SET event_type='revoked'")

    db_path = store.db_path
    expected_hash = rebuilt["state_hash"]
    store.close()
    reopened = SignalStore(db_path=db_path)
    try:
        assert reopened.rebuild_current_persona_state()["state_hash"] == expected_hash
    finally:
        reopened.close()


def test_persona_calibration_revoke_appends_successor_and_preserves_replay(store):
    save_persona_version_authorized(
        store,
        version=1,
        period_start="2026-07-01",
        period_end="2026-07-17",
        energy={"focus": 0.7},
        cognitive={"deduction": 0.8},
        value={"depth": 0.9},
        blindspot={},
        signal_count=7,
    )
    apply_action = store.prepare_persona_calibration_material_action(
        version=1,
        confirmed_at="2026-07-17T10:00:00+00:00",
        calibration_score=2.0,
        source_facts={"ratings": {"focus_depth": 2}},
        evidence_refs=("calibration-rating:focus_depth",),
    )
    assert apply_action is not None
    assert (
        store.record_persona_calibration(
            version=1,
            confirmed_at="2026-07-17T10:00:00+00:00",
            calibration_score=2.0,
            material_action=apply_action,
        )
        is True
    )

    revoke_action = store.prepare_persona_calibration_revoke_material_action(
        revoked_at="2026-07-17T11:00:00+00:00",
        source_facts={"reason": "user retracted calibration"},
        evidence_refs=("calibration-revoke:user-retracted",),
    )
    assert revoke_action is not None
    assert (
        store.revoke_persona_calibration(
            revoked_at="2026-07-17T11:00:00+00:00",
            material_action=revoke_action,
        )
        is True
    )

    latest = store.get_latest_persona_version()
    assert latest["version"] == 3
    assert latest["user_confirmed"] == 0
    assert latest["calibration_score"] is None
    conn = store._pool.get_conn()
    events = conn.execute(
        "SELECT event_id, supersedes_event_id, event_type FROM persona_calibration_events ORDER BY rowid"
    ).fetchall()
    assert [(row[1], row[2]) for row in events] == [(None, "applied"), (events[0][0], "revoked")]
    assert store.rebuild_current_persona_state()["status"] == "verified"
    with pytest.raises(ValueError, match="already revoked"):
        store.prepare_persona_calibration_revoke_material_action(
            revoked_at="2026-07-17T12:00:00+00:00",
            source_facts={"reason": "duplicate"},
            evidence_refs=("calibration-revoke:duplicate",),
        )


def test_persona_calibration_correction_supersedes_prior_event_without_mutation(store):
    save_persona_version_authorized(
        store,
        version=1,
        period_start="2026-07-01",
        period_end="2026-07-17",
        energy={},
        cognitive={},
        value={},
        blindspot={},
        signal_count=1,
    )
    first = store.prepare_persona_calibration_material_action(
        version=1,
        confirmed_at="2026-07-17T10:00:00+00:00",
        calibration_score=2.0,
        source_facts={"ratings": {"focus_depth": 2}},
        evidence_refs=("calibration-rating:focus_depth",),
    )
    assert first is not None
    assert (
        store.record_persona_calibration(
            version=1,
            confirmed_at="2026-07-17T10:00:00+00:00",
            calibration_score=2.0,
            material_action=first,
        )
        is True
    )

    correction = store.prepare_persona_calibration_material_action(
        version=2,
        confirmed_at="2026-07-17T11:00:00+00:00",
        calibration_score=5.0,
        source_facts={"ratings": {"focus_depth": 5}, "correction": True},
        evidence_refs=("calibration-correction:focus_depth",),
    )
    assert correction is not None
    assert (
        store.record_persona_calibration(
            version=2,
            confirmed_at="2026-07-17T11:00:00+00:00",
            calibration_score=5.0,
            material_action=correction,
        )
        is True
    )

    conn = store._pool.get_conn()
    events = conn.execute(
        "SELECT event_id, supersedes_event_id FROM persona_calibration_events ORDER BY rowid"
    ).fetchall()
    assert len(events) == 2
    assert events[1][1] == events[0][0]
    assert store.get_latest_persona_version()["calibration_score"] == 5.0
    assert store.rebuild_current_persona_state()["status"] == "verified"


def test_persona_blindspot_revoke_restores_pre_event_state(store):
    save_persona_version_authorized(
        store,
        version=1,
        period_start="2026-07-01",
        period_end="2026-07-17",
        energy={},
        cognitive={},
        value={},
        blindspot={"old": True},
        signal_count=1,
    )
    apply_action = store.prepare_blindspot_material_action(
        {"new": True},
        source_facts={"profile": {"new": True}},
        evidence_refs=("blindspot:apply",),
        created_at="2026-07-17T10:00:00+00:00",
    )
    assert apply_action is not None
    assert store.update_blindspot_profile({"new": True}, material_action=apply_action) is True

    revoke_action = store.prepare_persona_blindspot_revoke_material_action(
        revoked_at="2026-07-17T11:00:00+00:00",
        source_facts={"reason": "challenge disproved"},
        evidence_refs=("blindspot-revoke:disproved",),
    )
    assert revoke_action is not None
    assert (
        store.revoke_persona_blindspot(
            revoked_at="2026-07-17T11:00:00+00:00",
            material_action=revoke_action,
        )
        is True
    )

    latest = store.get_latest_persona_version()
    assert latest["version"] == 3
    assert latest["blindspot_profile"] == {"old": True}
    conn = store._pool.get_conn()
    assert [
        row[0]
        for row in conn.execute(
            "SELECT event_type FROM persona_blindspot_events ORDER BY rowid"
        ).fetchall()
    ] == ["applied", "revoked"]
    assert store.rebuild_current_persona_state()["status"] == "verified"


@pytest.mark.no_canonical_material_actions
def test_blindspot_producer_appends_exact_profile_revision(store):
    save_persona_version_authorized(
        store,
        version=7,
        period_start="2026-07-01",
        period_end="2026-07-17",
        energy={},
        cognitive={},
        value={},
        blindspot={},
        signal_count=3,
    )
    payload = {"confirmed": [], "suspected": [], "challenge_credit": 9.0}
    authorization = store.prepare_blindspot_material_action(
        payload,
        source_facts={"profile": payload, "challenge": {}},
        evidence_refs=("persona-blindspot-profile:current",),
        created_at="2026-07-17T10:00:00+00:00",
    )

    assert authorization is not None
    assert store.update_blindspot_profile(payload, material_action=authorization) is True
    assert store.get_latest_persona_version()["blindspot_profile"] == payload
    conn = store._pool.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM persona_revisions").fetchone()[0] == 2


def test_update_blindspot_profile_no_version_returns_false(store):
    """无画像版本时 update_blindspot_profile 应返回 False。"""
    ok = store.update_blindspot_profile({"gaps": []})
    assert ok is False


# ---------- 事件式注入 ----------


def test_handle_event_session_completed(store):
    """handle_event 处理 session_completed 事件应正确写入 session 信号。"""
    sid = store.handle_event(
        "session_completed",
        {
            "session_id": "evt-s1",
            "task_type": "coding/python",
            "duration_seconds": 120,
            "working_dir": "/tmp",
            "agent": "claude",
        },
    )
    assert sid > 0
    assert store.session_exists("evt-s1") is True


def test_handle_event_wiki_page_accessed(store):
    """handle_event 处理 wiki_page_accessed 事件应正确写入 knowledge 信号。"""
    wid = store.handle_event(
        "wiki_page_accessed",
        {"page_path": "concepts/test.md", "action_type": "access"},
    )
    assert wid > 0
    stats = store.get_signal_stats(days=30)
    assert stats["knowledge"] == 1


def test_handle_event_unknown_returns_none(store):
    """handle_event 对未知事件类型应返回 None。"""
    result = store.handle_event("unknown_event", {"foo": "bar"})
    assert result is None


# ---------- 单例 ----------


def test_get_signal_store_singleton(tmp_path, monkeypatch):
    """get_signal_store 应始终返回同一实例。"""
    # 强制使用临时路径，避免污染生产数据库
    from core.persona import psyche as _psyche_mod

    monkeypatch.setattr(_psyche_mod, "SIGNAL_DB_PATH", tmp_path / "singleton.db")
    SignalStore(
        db_path=tmp_path / "singleton.db",
        initialize_schema=True,
    ).close()
    # 重置单例
    monkeypatch.setattr(_psyche_mod, "_signal_store", None)

    s1 = get_signal_store()
    s2 = get_signal_store()
    assert s1 is s2
    s1.close()


def test_get_signal_store_recreates_when_default_path_changes(tmp_path, monkeypatch):
    """默认数据库路径变化时，单例不应复用旧配置下的 SignalStore。"""
    from core.persona import psyche as _psyche_mod

    first_db = tmp_path / "first.db"
    second_db = tmp_path / "second.db"
    monkeypatch.setattr(_psyche_mod, "_signal_store", None)
    monkeypatch.setattr(_psyche_mod, "SIGNAL_DB_PATH", first_db)
    SignalStore(db_path=first_db, initialize_schema=True).close()
    SignalStore(db_path=second_db, initialize_schema=True).close()

    s1 = get_signal_store()
    monkeypatch.setattr(_psyche_mod, "SIGNAL_DB_PATH", second_db)

    s2 = get_signal_store()
    assert s2 is not s1
    assert s2.db_path == second_db
    assert s2.session_exists("missing-session") is False
    s2.close()


def test_get_signal_store_ignores_magicmock_database_dir(tmp_path, monkeypatch):
    """配置对象缺少 database_dir 时，不应把 MagicMock 自动属性当成真实路径。"""
    import core.config as _config_mod
    from core.persona import psyche as _psyche_mod

    data_dir = tmp_path / "data"
    fake_cfg = MagicMock()
    fake_cfg.data_dir = data_dir
    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(_psyche_mod, "_signal_store", None)
    SignalStore(
        db_path=data_dir / "user_signals.db",
        config=fake_cfg,
        initialize_schema=True,
    ).close()

    store = get_signal_store()

    assert store.db_path == data_dir / "user_signals.db"
    assert "MagicMock" not in str(store.db_path)
    store.close()


def test_signal_store_does_not_recreate_missing_profile_schema_on_read(tmp_path):
    """A profile read must not perform hidden DDL when its schema is missing."""
    db = tmp_path / "missing-profile-schema.db"
    store = SignalStore(initialize_schema=True, db_path=db)
    conn = store._pool.get_conn()
    conn.execute("DROP TABLE profile_assertions")
    conn.commit()
    store._pool.release_transient_connections()

    profile = store.build_user_cognitive_profile_v2()

    assert profile["schema_version"] == "mnemos.user_cognitive_profile.v2"
    conn = store._pool.get_conn()
    assert profile["profile_assertions"] == []
    assert (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='profile_assertions'"
        ).fetchone()
        is None
    )
    store.close()


# ---------- 持久化 ----------


def test_signals_persist_across_reopen(tmp_path):
    """信号应在数据库关闭重新打开后仍然可读。"""
    db = tmp_path / "persist.db"
    s1 = SignalStore(initialize_schema=True, db_path=db)
    sig = SessionSignal(
        session_id="persist-s1",
        timestamp=datetime.now().isoformat(),
        task_type="coding",
    )
    s1.insert_session_signal(sig)
    s1.close()

    s2 = SignalStore(db_path=db)
    assert s2.session_exists("persist-s1") is True
    stats = s2.get_signal_stats(days=30)
    assert stats["session"] == 1
    s2.close()


# ---------- 去重 ----------


def test_duplicate_insert_returns_existing_id(store):
    """重复插入相同唯一键的信号应返回已有 id，不新增记录。"""
    ts = datetime.now().isoformat()
    sig = SessionSignal(
        session_id="dup-s1",
        timestamp=ts,
        task_type="coding",
    )
    sid1 = store.insert_session_signal(sig)
    sid2 = store.insert_session_signal(sig)
    assert sid1 == sid2

    stats = store.get_signal_stats(days=30)
    assert stats["session"] == 1
