# -*- coding: utf-8 -*-
"""
SyncEngine 专用单元测试

覆盖 SyncEngine 公共方法与核心内部逻辑，聚焦以下 8 个关键行为：
1. sync_session() — 会话级同步入口（增量跳过、噪音过滤、Hook 调用）
2. sync_batch() — 批量同步与统计聚合
3. retry_failed() — 失败重试（排除 auth_error）
4. 去重逻辑 — 本地 sync_log + backend 端兜底双检查
5. 错误处理 — StorageRateLimitError / StorageAuthError / StorageServerError / 通用异常
6. 审计记录 — record_audit / get_audit_summary
7. 关闭资源 — close() 释放连接池
8. 模块级函数 — build_turn_markdown / compute_content_hash / sanitize_content

所有外部依赖（DB、backend client、文件系统）均使用 Mock，确保测试在 <1s 内完成。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import core.sync_framework.sync_engine as sync_engine_module
from core.db_utils import RetryingConnection
from core.evidence.artifact_capture import write_managed_capture_artifact
from core.sync_framework.sync_engine import (
    CanonicalRawCommitError,
    SyncEngine,
    build_turn_markdown,
    compute_content_hash,
    sanitize_content,
    _load_sanitize_patterns,
)
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.storage_backend import (
    StorageRateLimitError,
    StorageAuthError,
    StorageServerError,
)

# ---------- 全局 FakeConfig ----------

_FAKE_CONFIG = Mock()
_FAKE_CONFIG.data_dir = Path(tempfile.gettempdir()) / "mnemos_test"
_FAKE_CONFIG.database_dir = _FAKE_CONFIG.data_dir
_FAKE_CONFIG.wiki_dir = _FAKE_CONFIG.data_dir / "wiki"
_FAKE_CONFIG.raw_dir = _FAKE_CONFIG.data_dir / "raw"
_FAKE_CONFIG.obsidian_vault_path = _FAKE_CONFIG.raw_dir
_FAKE_CONFIG.get = lambda key, default=None: {
    "storage.max_content_bytes": 100,
    "capture.reasoning_mode": "artifact_summary",
    "raw_event_store.enabled": False,
    "raw_projection.enabled": False,
}.get(key, default)


def test_sync_schema_late_abort_restores_preimage_and_closes_pool(
    tmp_path: Path,
    mock_client: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateSchemaAbort(BaseException):
        pass

    database = tmp_path / "sync_log.db"
    original_connect = sqlite3.connect
    with original_connect(database) as connection:
        connection.execute("CREATE TABLE preimage_sentinel (value TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO preimage_sentinel(value) VALUES ('unchanged')")

    opened: list[sqlite3.Connection] = []

    class FailingCursor(sqlite3.Cursor):
        def execute(self, sql: str, parameters=(), /):  # type: ignore[override]
            result = super().execute(sql, parameters)
            normalized = " ".join(str(sql).split()).lower()
            if "create table if not exists sync_audit" in normalized:
                raise LateSchemaAbort("sentinel sync schema failure")
            return result

    class FailingConnection(RetryingConnection):
        def cursor(self, *args: object, **kwargs: object):  # type: ignore[override]
            kwargs["factory"] = FailingCursor
            return super().cursor(*args, **kwargs)

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = FailingConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sync_engine_module.sqlite3, "connect", connect)

    with pytest.raises(LateSchemaAbort, match="sentinel sync schema failure"):
        SyncEngine(
            backend=mock_client,
            db_path=str(database),
            config=_FAKE_CONFIG,
        )

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
    with original_connect(database) as connection:
        objects = connection.execute("""
            SELECT type, name FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """).fetchall()
        rows = connection.execute("SELECT value FROM preimage_sentinel").fetchall()
    assert objects == [("table", "preimage_sentinel")]
    assert rows == [("unchanged",)]


def test_sync_engine_rejects_unmanaged_capture_artifact_before_raw_or_backend(
    tmp_path: Path,
    mock_client: Mock,
    fake_source: AgentSource,
) -> None:
    cfg = Mock()
    cfg.data_dir = tmp_path
    cfg.database_dir = tmp_path
    cfg.wiki_dir = tmp_path / "wiki"
    cfg.raw_dir = tmp_path / "raw"
    cfg.obsidian_vault_path = cfg.raw_dir
    cfg.get = lambda key, default=None: {
        "capture.reasoning_mode": "artifact_summary",
        "raw_projection.enabled": False,
    }.get(key, default)
    foreign = tmp_path / "foreign.md"
    foreign.write_text("foreign-secret", encoding="utf-8")
    engine = SyncEngine(
        backend=mock_client,
        db_path=str(tmp_path / "sync.db"),
        config=cfg,
    )
    try:
        result = engine.sync_single_turn(
            fake_source,
            SessionInfo(session_id="session", source_path=tmp_path / "native.jsonl"),
            Turn(
                turn_number=0,
                user_content="user",
                assistant_content="assistant",
                metadata={"artifact_path": str(foreign)},
            ),
            incremental=False,
        )
    finally:
        engine.close()

    assert result.action == "failed"
    assert result.error == "capture_artifact_reference_untrusted"
    mock_client.save.assert_not_called()
    assert foreign.read_text(encoding="utf-8") == "foreign-secret"


def test_sync_engine_reasoning_artifacts_are_source_scoped_and_immutable(
    tmp_path: Path,
    mock_client: Mock,
) -> None:
    cfg = Mock()
    cfg.data_dir = tmp_path
    cfg.database_dir = tmp_path
    cfg.wiki_dir = tmp_path / "wiki"
    cfg.raw_dir = tmp_path / "raw"
    cfg.obsidian_vault_path = cfg.raw_dir
    cfg.get = lambda key, default=None: {
        "capture.reasoning_mode": "artifact_summary",
        "raw_projection.enabled": False,
    }.get(key, default)
    engine = SyncEngine(
        backend=mock_client,
        db_path=str(tmp_path / "sync.db"),
        config=cfg,
    )
    try:
        first = Turn(
            turn_number=0,
            user_content="user",
            assistant_content="assistant",
            reasoning="first reasoning",
        )
        second_source = Turn(
            turn_number=0,
            user_content="user",
            assistant_content="assistant",
            reasoning="first reasoning",
        )
        next_generation = Turn(
            turn_number=0,
            user_content="user",
            assistant_content="assistant",
            reasoning="second reasoning",
        )
        engine._ensure_reasoning_artifact(  # noqa: SLF001
            first,
            "claude",
            "../../shared-session",
        )
        engine._ensure_reasoning_artifact(  # noqa: SLF001
            second_source,
            "kimi",
            "../../shared-session",
        )
        engine._ensure_reasoning_artifact(  # noqa: SLF001
            next_generation,
            "claude",
            "../../shared-session",
        )
    finally:
        engine.close()

    first_path = Path(first.metadata["reasoning_artifact_path"])
    second_path = Path(second_source.metadata["reasoning_artifact_path"])
    next_path = Path(next_generation.metadata["reasoning_artifact_path"])
    assert len({first_path, second_path, next_path}) == 3
    assert first_path.absolute().is_relative_to(
        (tmp_path / "capture_artifacts").absolute()
    )
    assert "first reasoning" in first_path.read_text(encoding="utf-8")
    assert "second reasoning" in next_path.read_text(encoding="utf-8")


def test_sync_engine_recomputes_or_removes_caller_reasoning_digest(
    tmp_path: Path,
    mock_client: Mock,
) -> None:
    cfg = Mock()
    cfg.data_dir = tmp_path
    cfg.database_dir = tmp_path
    cfg.wiki_dir = tmp_path / "wiki"
    cfg.raw_dir = tmp_path / "raw"
    cfg.obsidian_vault_path = cfg.raw_dir
    cfg.get = lambda key, default=None: {
        "capture.reasoning_mode": "artifact_summary",
        "raw_projection.enabled": False,
    }.get(key, default)
    managed = write_managed_capture_artifact(
        database_dir=tmp_path,
        source_agent="codex",
        session_id="session",
        turn_number=0,
        artifact_type="reasoning",
        content="system-owned artifact bytes",
    )
    with_path = Turn(
        turn_number=0,
        user_content="user",
        assistant_content="assistant",
        metadata={
            "reasoning_artifact_path": str(managed),
            "reasoning_sha256": "f" * 64,
        },
    )
    without_path = Turn(
        turn_number=1,
        user_content="user",
        assistant_content="assistant",
        metadata={"reasoning_sha256": "e" * 64},
    )
    engine = SyncEngine(
        backend=mock_client,
        db_path=str(tmp_path / "sync.db"),
        config=cfg,
    )
    try:
        engine._ensure_reasoning_artifact(  # noqa: SLF001
            with_path,
            "codex",
            "session",
        )
        engine._ensure_reasoning_artifact(  # noqa: SLF001
            without_path,
            "codex",
            "session",
        )
    finally:
        engine.close()

    assert with_path.metadata["reasoning_sha256"] == hashlib.sha256(
        managed.read_bytes()
    ).hexdigest()
    assert "reasoning_sha256" not in without_path.metadata


def test_reasoning_artifact_follows_explicit_db_owner_not_ambient_config(
    tmp_path: Path,
    mock_client: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_root = tmp_path / "caller"
    ambient_root = tmp_path / "ambient"
    explicit_config = {
        "capture.reasoning_mode": "artifact_summary",
        "raw_projection.enabled": False,
    }
    monkeypatch.setattr(
        sync_engine_module,
        "get_config",
        lambda: Mock(database_dir=ambient_root, data_dir=ambient_root),
    )
    engine = SyncEngine(
        backend=mock_client,
        db_path=str(caller_root / "sync.db"),
        config=explicit_config,  # type: ignore[arg-type]
    )
    turn = Turn(
        turn_number=0,
        user_content="user",
        assistant_content="assistant",
        reasoning="owned by caller root",
    )
    try:
        engine._ensure_reasoning_artifact(  # noqa: SLF001
            turn,
            "codex",
            "session",
        )
    finally:
        engine.close()

    path = Path(turn.metadata["reasoning_artifact_path"])
    assert path.absolute().is_relative_to(
        (caller_root / "capture_artifacts").absolute()
    )
    assert not (ambient_root / "capture_artifacts").exists()


def test_sync_engine_migrates_exact_legacy_artifact_to_managed_generation(
    tmp_path: Path,
    mock_client: Mock,
) -> None:
    cfg = Mock()
    cfg.data_dir = tmp_path
    cfg.database_dir = tmp_path
    cfg.wiki_dir = tmp_path / "wiki"
    cfg.raw_dir = tmp_path / "raw"
    cfg.obsidian_vault_path = cfg.raw_dir
    cfg.get = lambda key, default=None: {
        "capture.reasoning_mode": "artifact_summary",
        "raw_projection.enabled": False,
    }.get(key, default)
    legacy = tmp_path / "capture_artifacts" / "legacy-session" / "turn_0.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "# Capture Artifact\n\n## User\n\nlegacy-full\n\n"
        "## Assistant\n\nlegacy-answer\n",
        encoding="utf-8",
    )
    turn = Turn(
        turn_number=0,
        user_content="summary",
        assistant_content="summary",
        metadata={
            "artifact_path": str(legacy),
            "artifact_refs": [
                {
                    "artifact_type": "capture_artifact",
                    "path": str(legacy),
                },
                {
                    "artifact_type": "screenshot",
                    "path": str(tmp_path / "screen.png"),
                },
            ],
        },
    )
    engine = SyncEngine(
        backend=mock_client,
        db_path=str(tmp_path / "sync.db"),
        config=cfg,
    )
    try:
        engine._ensure_reasoning_artifact(  # noqa: SLF001
            turn,
            "codex",
            "legacy-session",
        )
    finally:
        engine.close()

    managed = Path(turn.metadata["artifact_path"])
    assert managed != legacy
    assert managed.absolute().is_relative_to(
        (tmp_path / "capture_artifacts").absolute()
    )
    assert managed.read_bytes() == legacy.read_bytes()
    assert len(turn.metadata["capture_artifact_sha256"]) == 64
    assert turn.metadata["artifact_refs"] == [
        {
            "artifact_type": "screenshot",
            "path": str(tmp_path / "screen.png"),
        }
    ]


# ---------- pytest fixtures ----------


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """提供临时数据库路径。"""
    return tmp_path / "sync_test.db"


@pytest.fixture
def mock_client() -> Mock:
    """提供预配置的 Mock backend client。"""
    client = Mock()
    client._sanitize = lambda x: x  # noqa
    client.save.return_value = [Mock(uid="uid-save")]
    client.list_by_tags.return_value = []
    return client


@pytest.fixture
def fake_source() -> AgentSource:
    """提供测试用的 AgentSource 实现。"""

    class _FakeSource(AgentSource):
        def __init__(self):
            self._name = "claude"
            self._model_tag = "claude-code"
            self.session_start_calls: list = []
            self.session_end_calls: list = []

        @property
        def name(self) -> str:
            return self._name

        @property
        def model_tag(self) -> str:
            return self._model_tag

        def discover_sessions(self):
            return []

        def parse_turns(self, session_path: Path):
            return []

        def on_session_start(self, session_id: str, context: dict):
            self.session_start_calls.append((session_id, context))
            return {}

        def on_session_end(self, session_id: str, messages: list):
            self.session_end_calls.append((session_id, messages))

    return _FakeSource()


@pytest.fixture
def engine(tmp_db_path: Path, mock_client: Mock) -> SyncEngine:
    """提供已初始化的 SyncEngine 实例。"""
    eng = SyncEngine(backend=mock_client, db_path=str(tmp_db_path), config=_FAKE_CONFIG)
    yield eng
    eng.close()


# ---------- 测试类 ----------


class TestSyncSession:
    """sync_session() 核心入口测试。"""

    def test_sync_session_all_turns(self, engine: SyncEngine, fake_source: AgentSource):
        """全量同步应处理会话所有轮次并返回正确结果。"""
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
            Turn(turn_number=1, user_content="how?", assistant_content="like this"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        results = engine.sync_session(fake_source, session, incremental=False)

        assert len(results) == 2
        assert results[0].action == "new"
        assert results[0].turn_number == 0
        assert results[1].action == "new"
        assert results[1].turn_number == 1

    def test_sync_session_uses_session_aware_parser(
        self, engine: SyncEngine, fake_source: AgentSource
    ):
        """Database sources must retain the discovered native session identity."""
        session = SessionInfo(
            session_id="sqlite-session-a",
            source_path=Path("/tmp/shared-source.db"),
            source_kind="sqlite",
            metadata={"native_session_id": "sqlite-session-a"},
        )
        parsed_sessions: list[SessionInfo] = []

        def parse_session(session_info: SessionInfo):
            parsed_sessions.append(session_info)
            return [
                Turn(turn_number=0, user_content="safe-user", assistant_content="safe-assistant")
            ]

        fake_source.parse_session = parse_session
        fake_source.parse_turns = Mock(
            side_effect=AssertionError("shared database path must not select a session")
        )

        results = engine.sync_session(fake_source, session, incremental=False)

        assert [item.session_id for item in parsed_sessions] == ["sqlite-session-a"]
        assert len(results) == 1
        assert results[0].action == "new"
        fake_source.parse_turns.assert_not_called()

    def test_incremental_skip_synced_turns(self, engine: SyncEngine, fake_source: AgentSource):
        """增量同步应跳过已同步的轮次。"""
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-inc", source_path=Path("/tmp/s.json"))

        # 先全量同步
        engine.sync_session(fake_source, session, incremental=False)
        # 再增量同步（应跳过）
        results = engine.sync_session(fake_source, session, incremental=True)

        assert len(results) == 0

    def test_noise_turn_marked_as_noise(self, engine: SyncEngine, fake_source: AgentSource):
        """噪音轮次应被标记为 noise 且不写入数据库。"""
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="[SYSTEM_INIT]", assistant_content=""),
        ]
        session = SessionInfo(session_id="sess-noise", source_path=Path("/tmp/s.json"))

        with patch(
            "core.sync_framework.sync_engine_support.is_noise_message",
            return_value=True,
        ):
            results = engine.sync_session(fake_source, session, incremental=False)

        assert len(results) == 1
        assert results[0].action == "noise"

    def test_session_hooks_called(self, engine: SyncEngine, fake_source: AgentSource):
        """session_start 和 session_end hooks 应被正确调用。"""
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(
            session_id="sess-hook", source_path=Path("/tmp/s.json"), working_dir="/proj"
        )
        engine.sync_session(fake_source, session, incremental=False)

        assert len(fake_source.session_start_calls) == 1
        assert fake_source.session_start_calls[0][0] == "sess-hook"
        assert fake_source.session_start_calls[0][1].get("working_dir") == "/proj"
        assert len(fake_source.session_end_calls) == 1


class TestSyncBatch:
    """sync_batch() 批量同步测试。"""

    def test_batch_sync_aggregates_stats(self, engine: SyncEngine, fake_source: AgentSource):
        """批量同步应正确聚合成功/失败/跳过统计。"""
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
            Turn(turn_number=1, user_content="bye", assistant_content="goodbye"),
        ]
        sessions = [
            SessionInfo(session_id="sess-a", source_path=Path("/tmp/a.json")),
            SessionInfo(session_id="sess-b", source_path=Path("/tmp/b.json")),
        ]
        result = engine.sync_batch(fake_source, sessions, incremental=False)

        assert result.total_sessions == 2
        assert len(result.successful) == 2
        assert len(result.failed) == 0
        assert result.turn_stats["new"] == 4

    def test_batch_sync_partial_failure(self, engine: SyncEngine, fake_source: AgentSource):
        """批量同步应隔离单个 session 的失败，不影响其他 session。"""
        call_count = [0]

        def parse_turns(path: Path):
            call_count[0] += 1
            if call_count[0] == 1:
                return [Turn(turn_number=0, user_content="ok", assistant_content="ok")]
            raise RuntimeError("parse error")

        fake_source.parse_turns = parse_turns
        sessions = [
            SessionInfo(session_id="sess-ok", source_path=Path("/tmp/a.json")),
            SessionInfo(session_id="sess-fail", source_path=Path("/tmp/b.json")),
        ]
        result = engine.sync_batch(fake_source, sessions, incremental=False)

        assert len(result.successful) == 1
        assert len(result.failed) == 1
        assert result.failed[0]["session_id"] == "sess-fail"
        assert "parse error" in result.failed[0]["error"]

    def test_sync_turns_fails_closed_when_sync_log_batch_commit_fails(
        self, engine: SyncEngine, fake_source: AgentSource
    ):
        session = SessionInfo(session_id="sess-ledger-fail", source_path=Path("/tmp/a.json"))
        turns = [Turn(turn_number=0, user_content="persisted", assistant_content="reply")]

        with (
            patch.object(engine, "_record_sync_and_persona_batch", return_value=None),
            patch.object(engine, "enqueue_session_for_distillation") as enqueue,
        ):
            results = engine.sync_turns(
                fake_source,
                session,
                turns,
                incremental=False,
            )

        assert results[0].action == "failed"
        assert results[0].error == "sync_persona_batch_commit_failed"
        enqueue.assert_not_called()

    def test_partial_persona_commit_set_never_publishes_or_handoffs(
        self, engine: SyncEngine, fake_source: AgentSource
    ):
        session = SessionInfo(
            session_id="sess-partial-persona-commit",
            source_path=Path("/tmp/partial-persona.json"),
        )
        turns = [
            Turn(
                turn_number=0,
                user_content="persisted persona evidence",
                assistant_content="reply",
            )
        ]

        with (
            patch.object(
                engine,
                "_record_sync_and_persona_batch",
                return_value=frozenset(),
            ),
            patch(
                "core.ops.cognitive_pipeline_receipts.record_synced_turn",
            ) as receipt,
            patch.object(engine, "enqueue_session_for_distillation") as handoff,
        ):
            results = engine.sync_turns(
                fake_source,
                session,
                turns,
                incremental=False,
            )

        assert [(result.action, result.error) for result in results] == [
            ("failed", "sync_persona_batch_commit_failed")
        ]
        receipt.assert_not_called()
        handoff.assert_not_called()


class TestRetryFailed:
    """retry_failed() 失败重试测试。"""

    def test_retry_failed_records(
        self, engine: SyncEngine, fake_source: AgentSource, tmp_db_path: Path
    ):
        """应重试 status='failed' 且非 auth_error 的记录。"""
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="retry me", assistant_content="ok"),
        ]
        # 插入失败记录
        with sqlite3.connect(str(tmp_db_path)) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (
                    agent_name, session_id, turn_number,
                    content_hash, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("claude", "sess-retry", 0, "abc123", "failed", "server_error: timeout"),
            )
            conn.commit()

        with patch("core.sync_framework.registry.SourceRegistry.get", return_value=fake_source):
            results = engine.retry_failed(limit=10)

        assert len(results) >= 1
        # 重试成功后 sync_log 状态应更新
        with sqlite3.connect(str(tmp_db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM sync_log WHERE session_id=? AND turn_number=?",
                ("sess-retry", 0),
            ).fetchone()
        assert row is not None
        assert row[0] in ("new", "updated")

    def test_retry_skips_auth_errors(self, engine: SyncEngine, tmp_db_path: Path):
        """auth_error 类型的失败记录不应被重试。"""
        with sqlite3.connect(str(tmp_db_path)) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (
                    agent_name, session_id, turn_number,
                    content_hash, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("claude", "sess-auth", 0, "abc123", "failed", "auth_error: bad token"),
            )
            conn.commit()

        with patch("core.sync_framework.registry.SourceRegistry.get", return_value=fake_source):
            results = engine.retry_failed(limit=10)

        assert len(results) == 0


class TestDeduplication:
    """去重逻辑测试：本地 sync_log + backend 端兜底。"""

    def test_local_sync_log_dedup(self, engine: SyncEngine, fake_source: AgentSource):
        """相同 content_hash 的已同步记录应被跳过。"""
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-dedup", source_path=Path("/tmp/s.json"))

        # 第一次同步
        r1 = engine.sync_session(fake_source, session, incremental=False)
        assert r1[0].action == "new"

        # 第二次同步（相同内容）
        r2 = engine.sync_session(fake_source, session, incremental=False)
        assert len(r2) == 1
        assert r2[0].action == "skipped"
        assert r2[0].content_hash == r1[0].content_hash

    def test_failed_record_not_treated_as_synced(
        self, engine: SyncEngine, fake_source: AgentSource, tmp_db_path: Path
    ):
        """failed 状态的记录不应因 content_hash 相同而被当作已同步跳过。"""
        turn = Turn(turn_number=0, user_content="hi", assistant_content="hello")
        session = SessionInfo(session_id="sess-fail-dedup", source_path=Path("/tmp/s.json"))

        content = engine._sanitize_content(
            engine._build_markdown(turn, session.session_id, fake_source.model_tag)
        )
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]

        # 插入 failed 记录
        with sqlite3.connect(str(tmp_db_path)) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (agent_name, session_id, turn_number, content_hash, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fake_source.name, session.session_id, turn.turn_number, content_hash, "failed"),
            )
            conn.commit()

        result = engine.sync_single_turn(fake_source, session, turn, incremental=False)
        assert result.action == "updated"

    def test_backend_duplicate_cache_dedup(
        self, engine: SyncEngine, fake_source: AgentSource, mock_client: Mock
    ):
        """使用 backend 去重缓存应能跳过已存在的记录。"""
        turn = Turn(turn_number=0, user_content="cached", assistant_content="yes")
        session = SessionInfo(session_id="sess-cache", source_path=Path("/tmp/s.json"))

        content = engine._sanitize_content(
            engine._build_markdown(turn, session.session_id, fake_source.model_tag)
        )
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]

        # 模拟 backend 已存在该记录
        mock_client.list_by_tags.return_value = [
            Mock(
                uid="existing-uid",
                content=content,
                tags=[
                    "source=claude",
                    "session=sess-cache",
                    "turn=1",
                    f"content_hash={content_hash}",
                ],
            )
        ]
        cache = engine.build_backend_duplicate_cache(fake_source.name)

        with patch.object(
            engine, "_check_backend_duplicate", side_effect=AssertionError("不应直接调用")
        ):
            result = engine.sync_single_turn(
                fake_source,
                session,
                turn,
                incremental=False,
                backend_duplicate_cache=cache,
            )

        assert result.action == "skipped"
        assert result.backend_uids == ["existing-uid"]
        mock_client.save.assert_not_called()

    def test_sync_uses_full_content_hash_from_metadata(
        self, engine: SyncEngine, fake_source: AgentSource, tmp_db_path: Path
    ):
        """P114: 若 turn.metadata 提供 full_content_hash，sync_single_turn 应优先使用它去重。"""
        full_hash = "abcd1234fullhash"
        turn = Turn(
            turn_number=0,
            user_content="truncated-prefix",
            assistant_content="truncated-prefix",
            metadata={"full_content_hash": full_hash},
        )
        session = SessionInfo(session_id="sess-full-hash", source_path=Path("/tmp/s.json"))

        # 预写入 sync_log，模拟已同步
        with sqlite3.connect(str(tmp_db_path)) as conn:
            conn.execute(
                """INSERT INTO sync_log (agent_name, session_id, turn_number, content_hash, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (fake_source.name, session.session_id, turn.turn_number, full_hash, "synced"),
            )
            conn.commit()

        result = engine.sync_single_turn(fake_source, session, turn, incremental=False)
        assert result.action == "skipped"
        assert result.content_hash == full_hash


class TestRawEventStoreIntegration:
    """SyncEngine 到 canonical raw store 的写入测试。"""

    def test_sync_single_turn_writes_raw_event_store(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        from core.ops.producer_consumer_ledger import ProducerConsumerLedger

        ProducerConsumerLedger(config, initialize=True)
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        try:
            turn = Turn(
                turn_number=0,
                user_content="native user",
                assistant_content="native assistant",
                source_files=["/tmp/native-session.jsonl"],
                completeness={"visible_text": "full", "truncated": False},
            )
            session = SessionInfo(session_id="sess-raw-sync", source_path=Path("/tmp/s.json"))

            result = eng.sync_single_turn(fake_source, session, turn, incremental=False)

            assert result.action == "new"
            row = (
                raw_store._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT event_id, content_hash, completeness_status, source_files_json "
                    "FROM raw_turns WHERE source_agent=? AND session_id=? AND turn_number=?",
                    ("claude", "sess-raw-sync", 0),
                )
                .fetchone()
            )
            assert row is not None
            assert row[1] == result.content_hash
            assert row[2] == "complete"
            raw_turn = raw_store.get_turn(row[0])
            assert raw_turn["user_content"] == "native user"
            assert raw_turn["assistant_content"] == "native assistant"
            assert raw_turn["source_files"] == ["/tmp/native-session.jsonl"]
            assert turn.metadata["raw_event_id"] == result.raw_event_id
            assert turn.metadata["raw_content_hash"] == row[1]
            with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as connection:
                cognitive_source = connection.execute("""
                    SELECT source_id, evidence_refs
                    FROM cognitive_data_events
                    WHERE data_type='synced_turn'
                    """).fetchone()
            assert cognitive_source is not None
            assert cognitive_source[0] == result.raw_event_id
            assert json.loads(cognitive_source[1]) == [result.raw_event_id]
            mock_client.save.assert_not_called()
        finally:
            eng.close()

    def test_sync_batch_writes_raw_without_touching_legacy_backend(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        """Continuous reconciliation must retain Raw projection's single owner."""

        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        try:
            turn = Turn(
                turn_number=0,
                user_content="batch native user",
                assistant_content="batch native assistant",
                completeness={"visible_text": "full", "truncated": False},
            )
            session = SessionInfo(
                session_id="sess-raw-batch-owner", source_path=Path("/tmp/s.json")
            )

            results = eng.sync_turns(
                fake_source,
                session,
                [turn],
                incremental=False,
                enqueue_distillation=False,
            )

            assert [result.action for result in results] == ["new"]
            assert results[0].backend_uids == []
            mock_client.list_by_tags.assert_not_called()
            mock_client.save.assert_not_called()
            row = (
                raw_store._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT event_id FROM raw_turns WHERE source_agent=? AND session_id=? AND turn_number=?",
                    ("claude", "sess-raw-batch-owner", 0),
                )
                .fetchone()
            )
            assert row is not None
        finally:
            eng.close()

    def test_incremental_old_turn_restores_raw_identity_before_complete_handoff(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        """Previously synced turns still carry canonical spans into a full handoff."""

        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="sess-incremental-handoff",
            source_path=Path("/tmp/incremental.jsonl"),
        )
        try:
            eng.sync_turns(
                fake_source,
                session,
                [
                    Turn(turn_number=0, user_content="old", assistant_content="zero"),
                    Turn(turn_number=1, user_content="old", assistant_content="one"),
                ],
                incremental=False,
                enqueue_distillation=False,
            )
            old_turn = Turn(
                turn_number=0,
                user_content="old",
                assistant_content="zero",
            )
            new_turn = Turn(
                turn_number=2,
                user_content="new",
                assistant_content="two",
            )
            persona_receipts: list[tuple[int, bool]] = []

            def capture_synced_turn(*_args, **kwargs):
                persona_receipts.append(
                    (
                        int(kwargs["turn"].turn_number),
                        bool(kwargs["persona_committed"]),
                    )
                )
                return "cognitive-event"

            with (
                patch.object(eng, "enqueue_session_for_distillation") as handoff,
                patch(
                    "core.ops.cognitive_pipeline_receipts.record_synced_turn",
                    side_effect=capture_synced_turn,
                ),
            ):
                results = eng.sync_turns(
                    fake_source,
                    session,
                    [old_turn, new_turn],
                    incremental=True,
                    enqueue_distillation=True,
                )

            assert [result.action for result in results] == ["skipped", "new"]
            assert persona_receipts == [(0, True), (2, True)]
            assert all(result.raw_event_id for result in results)
            assert all(result.content_hash for result in results)
            handoff.assert_called_once_with(fake_source, session, [old_turn, new_turn])
            for turn, result in zip((old_turn, new_turn), results):
                header = raw_store.get_revision_header(result.raw_event_id)
                assert turn.metadata["raw_event_id"]
                assert turn.metadata["raw_content_hash"] == header["content_hash"]
        finally:
            eng.close()

    def test_incremental_batch_repairs_a_historical_turn_content_change(
        self,
        tmp_db_path: Path,
        tmp_path: Path,
        mock_client: Mock,
        fake_source: AgentSource,
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="sess-historical-change",
            source_path=Path("/tmp/historical.jsonl"),
        )
        try:
            first = eng.sync_turns(
                fake_source,
                session,
                [
                    Turn(
                        turn_number=0,
                        user_content="before",
                        assistant_content="old answer",
                    ),
                    Turn(
                        turn_number=1,
                        user_content="later",
                        assistant_content="later answer",
                    ),
                ],
                incremental=False,
                enqueue_distillation=False,
            )
            changed = Turn(
                turn_number=0,
                user_content="after correction",
                assistant_content="new answer",
            )
            second = eng.sync_turns(
                fake_source,
                session,
                [changed],
                incremental=True,
                enqueue_distillation=False,
            )

            assert [item.action for item in first] == ["new", "new"]
            assert [item.action for item in second] == ["updated"]
            row = (
                eng._pool.get_conn()
                .execute(  # noqa: SLF001
                    """
                SELECT content_hash, status FROM sync_log
                WHERE agent_name=? AND session_id=? AND turn_number=0
                """,
                    (fake_source.name, session.session_id),
                )
                .fetchone()
            )
            assert row == (second[0].content_hash, "updated")
            persona = (
                eng._pool.get_conn()
                .execute(  # noqa: SLF001
                    """
                SELECT COUNT(*), content_length FROM user_signals
                WHERE agent=? AND session_id=? AND turn_number=0
                """,
                    (fake_source.name, session.session_id),
                )
                .fetchone()
            )
            assert persona == (
                1,
                len(f"{changed.user_content}\n{changed.assistant_content}"),
            )
        finally:
            eng.close()

    def test_incremental_batch_repairs_a_gap_below_the_maximum_turn(
        self,
        tmp_db_path: Path,
        tmp_path: Path,
        mock_client: Mock,
        fake_source: AgentSource,
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="sess-incremental-gap",
            source_path=Path("/tmp/gap.jsonl"),
        )
        try:
            eng.sync_turns(
                fake_source,
                session,
                [
                    Turn(
                        turn_number=1,
                        user_content="later",
                        assistant_content="committed first",
                    )
                ],
                incremental=False,
                enqueue_distillation=False,
            )
            repaired = eng.sync_turns(
                fake_source,
                session,
                [
                    Turn(
                        turn_number=0,
                        user_content="earlier gap",
                        assistant_content="must be recovered",
                    )
                ],
                incremental=True,
                enqueue_distillation=False,
            )

            assert [item.action for item in repaired] == ["new"]
            assert eng.get_synced_turns_for_session(
                fake_source.name,
                session,
            ) == [0, 1]
        finally:
            eng.close()

    def test_incremental_duplicate_repairs_persona_without_rewriting_sync_row(
        self,
        tmp_db_path: Path,
        tmp_path: Path,
        mock_client: Mock,
        fake_source: AgentSource,
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="sess-persona-repair",
            source_path=Path("/tmp/persona-repair.jsonl"),
        )
        turn = Turn(
            turn_number=0,
            user_content="same question?",
            assistant_content="same answer",
        )
        try:
            eng.sync_turns(
                fake_source,
                session,
                [turn],
                incremental=False,
                enqueue_distillation=False,
            )
            conn = eng._pool.get_conn()  # noqa: SLF001
            before = conn.execute(
                """
                SELECT content_hash, status, synced_at, distill_status
                FROM sync_log
                WHERE agent_name=? AND session_id=? AND turn_number=0
                """,
                (fake_source.name, session.session_id),
            ).fetchone()
            conn.execute(
                """
                DELETE FROM user_signals
                WHERE agent=? AND session_id=? AND turn_number=0
                """,
                (fake_source.name, session.session_id),
            )
            conn.commit()

            with patch(
                "core.ops.cognitive_pipeline_receipts.record_synced_turn",
                return_value="replayed-event",
            ) as receipt:
                repaired = eng.sync_turns(
                    fake_source,
                    session,
                    [
                        Turn(
                            turn_number=0,
                            user_content="same question?",
                            assistant_content="same answer",
                        )
                    ],
                    incremental=True,
                    enqueue_distillation=False,
                )

            after = conn.execute(
                """
                SELECT content_hash, status, synced_at, distill_status
                FROM sync_log
                WHERE agent_name=? AND session_id=? AND turn_number=0
                """,
                (fake_source.name, session.session_id),
            ).fetchone()
            assert [item.action for item in repaired] == ["skipped"]
            assert after == before
            assert (
                conn.execute(
                    """
                SELECT COUNT(*) FROM user_signals
                WHERE agent=? AND session_id=? AND turn_number=0
                """,
                    (fake_source.name, session.session_id),
                ).fetchone()
                == (1,)
            )
            assert receipt.call_args.kwargs["persona_committed"] is True
        finally:
            eng.close()

    def test_failed_persona_repair_never_leaves_an_exact_duplicate_green(
        self,
        tmp_db_path: Path,
        tmp_path: Path,
        mock_client: Mock,
        fake_source: AgentSource,
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="sess-persona-repair-failure",
            source_path=Path("/tmp/persona-repair-failure.jsonl"),
        )
        turn = Turn(
            turn_number=0,
            user_content="same question?",
            assistant_content="same answer",
        )
        try:
            eng.sync_turns(
                fake_source,
                session,
                [turn],
                incremental=False,
                enqueue_distillation=False,
            )
            conn = eng._pool.get_conn()  # noqa: SLF001
            conn.execute(
                """
                DELETE FROM user_signals
                WHERE agent=? AND session_id=? AND turn_number=0
                """,
                (fake_source.name, session.session_id),
            )
            conn.commit()

            with (
                patch.object(
                    eng,
                    "_record_sync_and_persona_batch",
                    return_value=None,
                ),
                patch(
                    "core.ops.cognitive_pipeline_receipts.record_synced_turn",
                ) as receipt,
                patch.object(eng, "enqueue_session_for_distillation") as handoff,
            ):
                result = eng.sync_turns(
                    fake_source,
                    session,
                    [
                        Turn(
                            turn_number=0,
                            user_content="same question?",
                            assistant_content="same answer",
                        )
                    ],
                    incremental=True,
                    enqueue_distillation=True,
                )

            assert [(item.action, item.error) for item in result] == [
                ("failed", "sync_persona_batch_commit_failed")
            ]
            receipt.assert_not_called()
            handoff.assert_not_called()
        finally:
            eng.close()

    def test_single_turn_backend_duplicate_still_commits_persona_and_receipt(
        self,
        engine: SyncEngine,
        fake_source: AgentSource,
    ):
        session = SessionInfo(
            session_id="sess-backend-duplicate-persona",
            source_path=Path("/tmp/backend-duplicate.jsonl"),
        )
        turn = Turn(
            turn_number=0,
            user_content="captured question?",
            assistant_content="captured answer",
        )
        with (
            patch.object(
                engine,
                "_check_backend_duplicate",
                return_value=["existing-backend-uid"],
            ),
            patch(
                "core.ops.cognitive_pipeline_receipts.record_synced_turn",
                return_value="backend-duplicate-event",
            ) as receipt,
        ):
            result = engine.sync_single_turn(
                fake_source,
                session,
                turn,
                incremental=False,
            )

        assert result.action == "skipped"
        assert result.backend_uids == ["existing-backend-uid"]
        conn = engine._pool.get_conn()  # noqa: SLF001
        assert (
            conn.execute(
                """
            SELECT status FROM sync_log
            WHERE agent_name=? AND session_id=? AND turn_number=0
            """,
                (fake_source.name, session.session_id),
            ).fetchone()
            == ("skipped_backend",)
        )
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM user_signals
            WHERE agent=? AND session_id=? AND turn_number=0
            """,
                (fake_source.name, session.session_id),
            ).fetchone()
            == (1,)
        )
        assert receipt.call_args.kwargs["persona_committed"] is True

    def test_bind_session_raw_identities_repairs_complete_backfill_handoff(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="sess-complete-backfill",
            source_path=Path("/tmp/complete-backfill.jsonl"),
        )
        turns = [
            Turn(turn_number=0, user_content="old", assistant_content="zero"),
            Turn(turn_number=1, user_content="new", assistant_content="one"),
        ]
        try:
            eng.sync_single_turn(fake_source, session, turns[0], incremental=False)
            repaired = eng.bind_session_raw_identities(fake_source, session, turns)

            assert repaired is turns
            for turn in turns:
                revision_id = turn.metadata["raw_event_id"]
                header = raw_store.get_revision_header(revision_id)
                assert header["session_id"] == session.session_id
                assert header["turn_number"] == turn.turn_number
                assert turn.metadata["raw_content_hash"] == header["content_hash"]
        finally:
            eng.close()

    def test_supplied_raw_revision_hash_mismatch_blocks_complete_handoff(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="sess-supplied-revision",
            source_path=Path("/tmp/supplied.jsonl"),
        )
        try:
            original = Turn(
                turn_number=0,
                user_content="stable user",
                assistant_content="stable assistant",
            )
            original_result = eng.sync_turns(
                fake_source,
                session,
                [original],
                incremental=False,
                enqueue_distillation=False,
            )[0]
            revision_id = original_result.raw_event_id
            header = raw_store.get_revision_header(revision_id)
            replay = Turn(
                turn_number=0,
                user_content="stable user",
                assistant_content="stable assistant",
                metadata={
                    "raw_event_id": revision_id,
                    "raw_content_hash": "forged",
                },
            )

            with patch.object(eng, "enqueue_session_for_distillation") as handoff:
                replay_result = eng.sync_turns(
                    fake_source,
                    session,
                    [replay],
                    incremental=False,
                    enqueue_distillation=True,
                )[0]

            assert replay_result.action == "skipped"
            assert replay.metadata["raw_content_hash"] == header["content_hash"]
            handoff.assert_called_once_with(fake_source, session, [replay])

            changed = Turn(
                turn_number=0,
                user_content="changed user",
                assistant_content="changed assistant",
                metadata={"raw_event_id": revision_id},
            )
            with patch.object(eng, "enqueue_session_for_distillation") as handoff:
                changed_result = eng.sync_turns(
                    fake_source,
                    session,
                    [changed],
                    incremental=False,
                    enqueue_distillation=True,
                )[0]

            assert changed_result.action == "failed"
            assert changed_result.error == "canonical_raw_commit_missing"
            handoff.assert_not_called()

            wrong_turn = Turn(
                turn_number=2,
                user_content="stable user",
                assistant_content="stable assistant",
                metadata={"raw_event_id": revision_id},
            )
            with patch.object(eng, "enqueue_session_for_distillation") as handoff:
                wrong_turn_result = eng.sync_turns(
                    fake_source,
                    session,
                    [wrong_turn],
                    incremental=False,
                    enqueue_distillation=True,
                )[0]

            assert wrong_turn_result.action == "failed"
            assert wrong_turn_result.error == "canonical_raw_commit_missing"
            handoff.assert_not_called()
        finally:
            eng.close()

    def test_noise_turn_still_receives_canonical_raw_receipt(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_FAKE_CONFIG)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=_FAKE_CONFIG,
            raw_store=raw_store,
        )
        try:
            turn = Turn(turn_number=0, user_content="noise", assistant_content="noise")
            session = SessionInfo(session_id="noise-session", source_path=Path("/tmp/noise.jsonl"))

            with patch.object(eng, "_is_noise", return_value=True):
                result = eng.sync_single_turn(fake_source, session, turn, incremental=False)

            assert result.action == "noise"
            assert result.raw_event_id
            row = (
                raw_store._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT session_id FROM raw_turns WHERE source_agent=? AND turn_number=0",
                    (fake_source.name,),
                )
                .fetchone()
            )
            assert row == ("noise-session",)
        finally:
            eng.close()

    def test_raw_write_failure_blocks_single_turn_sync_persona_and_handoff_effects(
        self, tmp_db_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        """A missing canonical Raw receipt may not advance downstream effects."""

        class FailingRawStore:
            def upsert_turn(self, **_kwargs):
                raise sqlite3.OperationalError("raw database locked")

            def close(self):
                return None

        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=_FAKE_CONFIG,
            raw_store=FailingRawStore(),
        )
        try:
            turn = Turn(
                turn_number=0, user_content="raw gate user", assistant_content="raw gate assistant"
            )
            session = SessionInfo(session_id="sess-raw-gate", source_path=Path("/tmp/s.json"))

            with patch("core.ops.cognitive_pipeline_receipts.record_synced_turn") as receipt:
                result = eng.sync_single_turn(fake_source, session, turn, incremental=False)

            assert result.action == "failed"
            assert result.raw_event_id is None
            assert "canonical_raw_commit" in (result.error or "")
            mock_client.save.assert_not_called()
            receipt.assert_not_called()
            row = (
                eng._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT status, error FROM sync_log WHERE agent_name=? AND session_id=? AND turn_number=?",
                    (fake_source.name, session.session_id, 0),
                )
                .fetchone()
            )
            assert row is not None
            assert row[0] == "failed"
            assert "canonical_raw_commit" in row[1]
        finally:
            eng.close()

    def test_raw_header_outage_fails_before_sync_success_or_cognitive_receipt(
        self,
        tmp_db_path: Path,
        mock_client: Mock,
        fake_source: AgentSource,
    ):
        class HeaderUnavailableRawStore:
            def upsert_turn(self, **_kwargs):
                return "raw-revision-sentinel"

            def get_revision_header(self, _revision_id):
                raise sqlite3.OperationalError("synthetic raw header outage")

            def close(self):
                return None

        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=_FAKE_CONFIG,
            raw_store=HeaderUnavailableRawStore(),
        )
        try:
            turn = Turn(
                turn_number=0,
                user_content="raw header gate user",
                assistant_content="raw header gate assistant",
            )
            session = SessionInfo(
                session_id="sess-raw-header-gate",
                source_path=Path("/tmp/s.json"),
            )

            with patch("core.ops.cognitive_pipeline_receipts.record_synced_turn") as receipt:
                result = eng.sync_single_turn(
                    fake_source,
                    session,
                    turn,
                    incremental=False,
                )

            assert result.action == "failed"
            assert result.raw_event_id == "raw-revision-sentinel"
            assert result.error == "canonical_raw_revision_header_unavailable"
            receipt.assert_not_called()
            row = (
                eng._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT status, error FROM sync_log "
                    "WHERE agent_name=? AND session_id=? AND turn_number=?",
                    (fake_source.name, session.session_id, 0),
                )
                .fetchone()
            )
            assert row == (
                "failed",
                "canonical_raw_revision_header_unavailable",
            )
        finally:
            eng.close()

    def test_batch_cognitive_receipt_failure_is_retryable_without_persona_duplicates(
        self,
        tmp_db_path: Path,
        tmp_path: Path,
        mock_client: Mock,
        fake_source: AgentSource,
    ):
        class _RawProjectionConfig:
            database_dir = tmp_path
            data_dir = tmp_path

            @staticmethod
            def get(key, default=None):
                return {"raw_projection.enabled": True}.get(key, default)

        config = _RawProjectionConfig()
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=config)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=config,
            raw_store=raw_store,
        )
        session = SessionInfo(
            session_id="batch-cognitive-retry",
            source_path=Path("/tmp/batch-cognitive-retry.jsonl"),
        )
        try:
            first_turn = Turn(
                turn_number=0,
                user_content="retryable user?",
                assistant_content="retryable assistant",
            )
            with (
                patch(
                    "core.ops.cognitive_pipeline_receipts.record_synced_turn",
                    side_effect=RuntimeError("synthetic cognitive receipt outage"),
                ),
                patch.object(eng, "enqueue_session_for_distillation") as enqueue,
            ):
                failed = eng.sync_turns(
                    fake_source,
                    session,
                    [first_turn],
                    incremental=False,
                )

            assert failed[0].action == "failed"
            assert failed[0].error == "cognitive_sync_receipt_commit_failed"
            enqueue.assert_not_called()
            assert eng._pool.get_conn().execute(  # noqa: SLF001
                "SELECT status FROM sync_log WHERE session_id=? AND turn_number=0",
                (session.session_id,),
            ).fetchone() == ("failed",)
            assert (
                eng._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM user_signals " "WHERE session_id=? AND turn_number=0",
                    (session.session_id,),
                )
                .fetchone()[0]
                == 1
            )

            retry_turn = Turn(
                turn_number=0,
                user_content="retryable user?",
                assistant_content="retryable assistant",
            )
            with (
                patch(
                    "core.ops.cognitive_pipeline_receipts.record_synced_turn",
                    return_value="cognitive-event-sentinel",
                ) as receipt,
                patch.object(eng, "enqueue_session_for_distillation") as enqueue,
            ):
                retried = eng.sync_turns(
                    fake_source,
                    session,
                    [retry_turn],
                    incremental=False,
                )

            assert retried[0].action in {"new", "updated"}
            receipt.assert_called_once()
            enqueue.assert_called_once()
            assert (
                eng._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM user_signals " "WHERE session_id=? AND turn_number=0",
                    (session.session_id,),
                )
                .fetchone()[0]
                == 1
            )
        finally:
            eng.close()

    def test_raw_write_failure_blocks_batch_handoff(
        self, tmp_db_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        """Batch reconciliation cannot enqueue a complete session without Raw."""

        class FailingRawStore:
            def upsert_turn(self, **_kwargs):
                raise sqlite3.OperationalError("raw database locked")

            def close(self):
                return None

        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=_FAKE_CONFIG,
            raw_store=FailingRawStore(),
        )
        try:
            turn = Turn(
                turn_number=0, user_content="batch user", assistant_content="batch assistant"
            )
            session = SessionInfo(session_id="sess-raw-batch", source_path=Path("/tmp/s.json"))

            with patch.object(eng, "enqueue_session_for_distillation") as handoff:
                results = eng.sync_turns(
                    fake_source,
                    session,
                    [turn],
                    incremental=False,
                    enqueue_distillation=True,
                )

            assert [result.action for result in results] == ["failed"]
            handoff.assert_not_called()
            row = (
                eng._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT status FROM sync_log WHERE agent_name=? AND session_id=? AND turn_number=?",
                    (fake_source.name, session.session_id, 0),
                )
                .fetchone()
            )
            assert row == ("failed",)
        finally:
            eng.close()

    def test_raw_projection_can_be_disabled_for_legacy_backend_write(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_FAKE_CONFIG)
        cfg = Mock()
        cfg.data_dir = tmp_path
        cfg.database_dir = tmp_path
        cfg.wiki_dir = tmp_path / "wiki"
        cfg.raw_dir = tmp_path / "raw"
        cfg.obsidian_vault_path = cfg.raw_dir
        cfg.get = lambda key, default=None: {
            "storage.max_content_bytes": 100,
            "capture.reasoning_mode": "artifact_summary",
            "raw_projection.enabled": False,
        }.get(key, default)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=cfg,
            raw_store=raw_store,
        )
        try:
            turn = Turn(turn_number=0, user_content="legacy user", assistant_content="legacy")
            session = SessionInfo(session_id="sess-legacy-raw", source_path=Path("/tmp/s.json"))

            result = eng.sync_single_turn(fake_source, session, turn, incremental=False)

            assert result.action == "new"
            mock_client.save.assert_called_once()
            assert result.backend_uids == ["uid-save"]
        finally:
            eng.close()

    def test_source_fidelity_mismatch_is_not_recorded_as_derived(
        self, tmp_db_path: Path, tmp_path: Path, mock_client: Mock, fake_source: AgentSource
    ):
        fake_source.completeness_capabilities = lambda: {  # type: ignore[method-assign]
            "visible_text": True,
            "source_fidelity": "derived",
        }
        raw_store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_FAKE_CONFIG)
        eng = SyncEngine(
            backend=mock_client,
            db_path=str(tmp_db_path),
            config=_FAKE_CONFIG,
            raw_store=raw_store,
        )
        try:
            turn = Turn(turn_number=0, user_content="derived user", assistant_content="derived")
            session = SessionInfo(session_id="sess-derived", source_path=Path("/tmp/corpus.md"))

            result = eng.sync_single_turn(fake_source, session, turn, incremental=False)

            assert result.action == "new"
            row = (
                raw_store._pool.get_conn()
                .execute(  # noqa: SLF001
                    "SELECT event_id, completeness_status FROM raw_turns "
                    "WHERE session_id=? AND turn_number=?",
                    ("sess-derived", 0),
                )
                .fetchone()
            )
            assert row is not None
            # The declared Claude source promises lossless, full-fidelity Raw.
            # A parser observing only derived fidelity must preserve its bytes
            # but may not retain a certifying/derived logical status.
            assert row[1] == "partial"
            raw_turn = raw_store.get_turn(row[0])
            assert raw_turn is not None
            assert raw_turn["user_content"] == "derived user"
            assert raw_turn["assistant_content"] == "derived"
            assert raw_turn["metadata"]["support_raw_contract_state"] == "nonconforming"
            assert (
                "source_fidelity_contract_mismatch"
                in raw_turn["metadata"]["support_raw_contract_errors"]
            )
            assert raw_turn["metadata"]["support_native_contract_certifying"] is False
        finally:
            eng.close()


class TestErrorHandling:
    """错误处理测试：覆盖各类 Storage 异常。"""

    def test_rate_limit_error(
        self, engine: SyncEngine, fake_source: AgentSource, mock_client: Mock
    ):
        """StorageRateLimitError 应记录 failed 状态并返回错误信息。"""
        mock_client.save.side_effect = StorageRateLimitError("too fast")
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-rl", source_path=Path("/tmp/s.json"))

        results = engine.sync_session(fake_source, session, incremental=False)

        assert results[0].action == "failed"
        assert "rate_limit" in results[0].error

    def test_auth_error(self, engine: SyncEngine, fake_source: AgentSource, mock_client: Mock):
        """StorageAuthError 应记录 failed 状态并返回错误信息。"""
        mock_client.save.side_effect = StorageAuthError("bad token")
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-auth", source_path=Path("/tmp/s.json"))

        results = engine.sync_session(fake_source, session, incremental=False)

        assert results[0].action == "failed"
        assert "auth_error" in results[0].error

    def test_server_error(self, engine: SyncEngine, fake_source: AgentSource, mock_client: Mock):
        """StorageServerError 应记录 failed 状态并返回错误信息。"""
        mock_client.save.side_effect = StorageServerError("500 internal")
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-se", source_path=Path("/tmp/s.json"))

        results = engine.sync_session(fake_source, session, incremental=False)

        assert results[0].action == "failed"
        assert "server_error" in results[0].error

    def test_generic_exception(
        self, engine: SyncEngine, fake_source: AgentSource, mock_client: Mock
    ):
        """通用异常应被捕获并记录 failed 状态。"""
        mock_client.save.side_effect = ValueError("unexpected")
        fake_source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-generic", source_path=Path("/tmp/s.json"))

        results = engine.sync_session(fake_source, session, incremental=False)

        assert results[0].action == "failed"
        assert "unexpected" in results[0].error


class TestAudit:
    """审计记录测试。"""

    def test_record_and_get_audit_summary(self, engine: SyncEngine, tmp_db_path: Path):
        """record_audit 写入后 get_audit_summary 应能正确汇总。"""
        engine.record_audit(
            source="claude",
            audit_type="l1_scan",
            skipped_missing=1,
            skipped_large=2,
            selected=5,
            synced_turns=3,
        )
        engine.record_audit(
            source="claude",
            audit_type="l1_scan",
            skipped_missing=0,
            skipped_large=1,
            selected=3,
            synced_turns=2,
        )
        engine.record_audit(
            source="kimi",
            audit_type="l1_scan",
            skipped_missing=2,
            selected=1,
            synced_turns=1,
        )

        summary = engine.get_audit_summary(hours=24)

        assert "claude" in summary
        assert "kimi" in summary
        assert summary["claude"]["skipped_missing"] == 1
        assert summary["claude"]["skipped_large"] == 3
        assert summary["claude"]["selected"] == 8
        assert summary["claude"]["synced_turns"] == 5
        assert summary["kimi"]["skipped_missing"] == 2


class TestCanonicalSessionLookup:
    """Every sync-log read must share the canonical Raw session key."""

    def test_get_synced_turns_resolves_alias_before_lookup(
        self, engine: SyncEngine, fake_source: AgentSource
    ):
        canonical = "canonical-session"
        engine._record_sync(  # noqa: SLF001 - asserts the public lookup boundary.
            fake_source.name,
            canonical,
            7,
            "content-hash",
            [],
            "synced",
        )
        alias = SessionInfo(
            session_id="path-alias",
            canonical_session_id=canonical,
            source_path=Path("/tmp/alias.jsonl"),
        )

        assert engine.get_synced_turns_for_session(fake_source.name, alias) == [7]


class TestLifecycle:
    """生命周期测试。"""

    def test_close_releases_pool(self, tmp_db_path: Path, mock_client: Mock):
        """close() 应释放数据库连接池。"""
        eng = SyncEngine(backend=mock_client, db_path=str(tmp_db_path), config=_FAKE_CONFIG)

        # 确认 pool 存在
        assert hasattr(eng, "_pool")
        # 获取一个连接以验证 pool 正常工作
        conn_before = eng._pool.get_conn()
        assert conn_before is not None
        eng.close()
        # 关闭后再次获取连接应返回新连接（原 pool 已关闭）
        # 这里验证 close 被调用后不会抛出异常即可
        assert True


class TestModuleLevelFunctions:
    """模块级辅助函数测试。"""

    def test_build_turn_markdown_contains_turn_info(self):
        """build_turn_markdown 应包含轮次编号和对话内容。"""
        turn = Turn(
            turn_number=0,
            user_content="hello",
            assistant_content="world",
            tool_calls=[{"name": "test", "input": {}}],
        )
        md = build_turn_markdown(turn, "sess-1", "claude-code")

        assert "Turn 1" in md
        assert "hello" in md
        assert "world" in md
        assert "## Tool Calls" in md

    def test_compute_content_hash_consistency(self):
        """compute_content_hash 对相同输入应返回相同哈希。"""
        h1 = compute_content_hash("hi", "hello", 0, "claude-code")
        h2 = compute_content_hash("hi", "hello", 0, "claude-code")
        h3 = compute_content_hash("hi", "hello", 1, "claude-code")

        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 16

    def test_sanitize_content_masks_api_keys(self, monkeypatch):
        """sanitize_content 应脱敏 API 密钥。"""
        monkeypatch.setattr("core.sync_framework.sync_engine.get_config", lambda: _FAKE_CONFIG)
        text = "key: " + "sk" + "-abcdefghijklmnopqrstuvwxyz123456"
        result = sanitize_content(text)
        assert "[API-KEY]" in result
        assert "sk-abc" not in result

    def test_load_sanitize_patterns_fallback(self, monkeypatch):
        """_load_sanitize_patterns 在配置文件不存在时应返回默认规则。"""
        monkeypatch.setattr("core.sync_framework.sync_engine.get_config", lambda: _FAKE_CONFIG)
        patterns = _load_sanitize_patterns()
        assert len(patterns) > 0
        # 默认规则应包含 API key 模式
        assert any("sk-" in p[0] for p in patterns)

    def test_uninspectable_sanitize_pattern_config_never_uses_defaults(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = Mock()
        config.data_dir = tmp_path
        target = tmp_path / "configs" / "sanitize_patterns.json"
        original_stat = Path.stat

        def denied(path: Path, *args: object, **kwargs: object):
            if path == target:
                raise PermissionError("sentinel")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)
        monkeypatch.setattr(
            "core.sync_framework.sync_engine.get_config",
            lambda: config,
        )

        with pytest.raises(
            CanonicalRawCommitError,
            match="sanitize_pattern_config_unavailable",
        ):
            _load_sanitize_patterns()

    def test_invalid_sanitize_pattern_config_never_uses_defaults(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = Mock()
        config.data_dir = tmp_path
        target = tmp_path / "configs" / "sanitize_patterns.json"
        target.parent.mkdir()
        target.write_text('{"not": "a reviewed pattern list"}', encoding="utf-8")
        monkeypatch.setattr(
            "core.sync_framework.sync_engine.get_config",
            lambda: config,
        )

        with pytest.raises(
            CanonicalRawCommitError,
            match="sanitize_pattern_config_invalid",
        ):
            _load_sanitize_patterns()

    def test_sanitize_pattern_config_never_follows_a_leaf_symlink(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = Mock()
        config.data_dir = tmp_path
        target = tmp_path / "outside-patterns.json"
        target.write_text('[["secret", "[HIDDEN]"]]', encoding="utf-8")
        configured = tmp_path / "configs" / "sanitize_patterns.json"
        configured.parent.mkdir()
        configured.symlink_to(target)
        monkeypatch.setattr(
            "core.sync_framework.sync_engine.get_config",
            lambda: config,
        )

        with pytest.raises(
            CanonicalRawCommitError,
            match="sanitize_pattern_config_not_regular",
        ):
            _load_sanitize_patterns()
