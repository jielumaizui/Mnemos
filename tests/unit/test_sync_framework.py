"""
通用同步框架 (core/sync_framework/) 单元测试

覆盖项：
- AgentSource ABC + SessionInfo/Turn/SyncResult dataclasses
- SyncEngine 8 步流水线（增量跳过、噪音过滤、脱敏、去重、标签组装、存储分片、状态记录、信号采集）
- SyncEngine.sync_batch 批量同步与统计
- SyncEngine.retry_failed 失败重试（排除 auth_error）
- AgentRegistry 注册/发现/获取
- PathDiscover 路径发现（4 层回退）
- BaseTrigger 指数退避与错误隔离
- WatchdogTrigger 去抖动与生命周期
- PollingTrigger 轮询与状态持久化
- FileIngestor 文件提取与保存
"""

import hashlib
import json
import time
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, MagicMock, PropertyMock


import unittest

from integrations.sources.claude_source import ClaudeSource
from core.mnemos_bus import publish_event as _REAL_PUBLISH_EVENT

_FAKE_CONFIG = {
    "data_dir": Path(tempfile.gettempdir()) / "mnemos_test",
    "raw_projection.enabled": False,
    "storage.obsidian.daily_size_threshold": 819200,
    "storage.ingest_batch_size": 10,
    "storage.ingest_batch_interval": 0,
    "storage.query_cache_ttl": 30,
}

with (
    patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG),
    patch("core.sync_framework.triggers.get_config", return_value=_FAKE_CONFIG),
):
    from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn, SyncResult
    from core.sync_framework.sync_engine import SyncEngine
    from core.sync_framework.registry import (
        PathDiscover,
        PathDiscoveryUnavailableError,
        SourceRegistry,
    )

    AgentRegistry = SourceRegistry  # 向后兼容
    from core.sync_framework.file_ingestor import FileIngestor

    try:
        from core.sync_framework.triggers import BaseTrigger, WatchdogTrigger, PollingTrigger

        _WATCHDOG_AVAILABLE = True
    except ImportError:
        _WATCHDOG_AVAILABLE = False


class FakeAgentSource(AgentSource):
    """测试用的 AgentSource 实现"""

    def __init__(self, name="claude", model_tag="claude-code", data_dir=None):
        self._name = name
        self._model_tag = model_tag
        self._data_dir = Path(data_dir) if data_dir else None
        self.session_start_calls = []
        self.session_end_calls = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_tag(self) -> str:
        return self._model_tag

    @property
    def data_dir(self):
        return self._data_dir

    def discover_sessions(self):
        return []

    def parse_turns(self, session_path: Path):
        return []

    def on_session_start(self, session_id: str, context: dict):
        self.session_start_calls.append((session_id, context))
        return {}

    def on_session_end(self, session_id: str, messages: list):
        self.session_end_calls.append((session_id, messages))


class TestDataclasses(unittest.TestCase):
    def test_session_info_defaults(self):
        """SessionInfo 默认值正确"""
        s = SessionInfo(session_id="s1", source_path=Path("/tmp/a.json"))
        self.assertEqual(s.session_id, "s1")
        self.assertIsNone(s.working_dir)
        self.assertIsNone(s.mtime)
        self.assertEqual(s.metadata, {})

    def test_turn_defaults(self):
        """Turn 默认值正确"""
        t = Turn(turn_number=0, user_content="hi", assistant_content="hello")
        self.assertEqual(t.metadata, {})
        self.assertIsNone(t.timestamp)

    def test_sync_result_defaults(self):
        """SyncResult 默认值正确"""
        r = SyncResult(session_id="s1", turn_number=0, action="new")
        self.assertEqual(r.backend_uids, [])
        self.assertIsNone(r.content_hash)
        self.assertIsNone(r.error)


class TestSyncEngineInit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "sync_test.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    @patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG)
    def test_creates_db_and_tables(self, _mock_cfg):
        """初始化时创建 sync_log 和 user_signals 表"""
        mock_backend = Mock()
        _ = SyncEngine(backend=mock_backend, db_path=str(self.db_path))
        self.assertTrue(self.db_path.exists())
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cursor.fetchall()}
            self.assertIn("sync_log", tables)
            self.assertIn("user_signals", tables)

    @patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG)
    def test_uses_provided_backend(self, _mock_cfg):
        """使用传入的 StorageBackend"""
        mock_backend = Mock()
        engine = SyncEngine(backend=mock_backend, db_path=str(self.db_path))
        self.assertIs(engine.backend, mock_backend)

    @patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG)
    def test_shard_threshold_from_config(self, _mock_cfg):
        """分片阈值从 Config 读取"""
        mock_backend = Mock()
        engine = SyncEngine(backend=mock_backend, db_path=str(self.db_path))
        self.assertEqual(engine._shard_threshold, 819200)

    @patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG)
    def test_mapping_config_creates_adjacent_canonical_raw_store(self, _mock_cfg):
        """Legacy mapping config still receives a real canonical Raw store, never a gate bypass."""
        engine = SyncEngine(backend=Mock(), db_path=str(self.db_path))

        self.assertIsNotNone(engine.raw_store)
        self.assertEqual(engine.raw_store.db_path, self.db_path.parent / "raw_events.db")


class TestSyncEngineSessionSync(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "sync_test.db"
        self.mock_backend = Mock()
        self.mock_backend._sanitize = lambda x: x  # noqa
        self.mock_backend.save.return_value = [Mock(uid="uid-1")]
        self.mock_backend.list_by_tags.return_value = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_engine(self):
        with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
            return SyncEngine(backend=self.mock_backend, db_path=str(self.db_path))

    def _get_save_tags(self):
        """辅助：从 mock save 的位置参数中提取 tags"""
        call = self.mock_backend.save.call_args
        if call and call[0] and len(call[0]) >= 2:
            return call[0][1]
        return call[1].get("tags", []) if call and call[1] else []

    def _get_save_content(self):
        """辅助：从 mock save 的位置参数中提取 content"""
        call = self.mock_backend.save.call_args
        if call and call[0]:
            return call[0][0]
        return call[1].get("content", "") if call and call[1] else ""

    def test_backend_duplicate_lookup_failure_never_becomes_no_match(self):
        from core.sync_framework.sync_engine_support import (
            BackendDuplicateStateUnavailableError,
        )

        engine = self._make_engine()
        self.mock_backend.list_by_tags.side_effect = OSError("unavailable")

        with self.assertRaisesRegex(
            BackendDuplicateStateUnavailableError,
            "backend_duplicate_lookup_unavailable",
        ):
            engine._check_backend_duplicate(
                "claude",
                "session",
                0,
                "hash",
            )

    def test_session_duplicate_cache_failure_never_becomes_empty_cache(self):
        from core.sync_framework.sync_engine_support import (
            BackendDuplicateStateUnavailableError,
        )

        engine = self._make_engine()
        self.mock_backend.list_by_tags.side_effect = OSError("unavailable")

        with self.assertRaisesRegex(
            BackendDuplicateStateUnavailableError,
            "backend_session_duplicate_cache_unavailable",
        ):
            engine._build_backend_duplicate_cache(
                FakeAgentSource(),
                "session",
            )

    def test_sync_session_all_turns(self):
        """同步会话所有轮次"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
            Turn(turn_number=1, user_content="how?", assistant_content="like this"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        results = engine.sync_session(source, session, incremental=False)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].action, "new")
        self.assertEqual(results[1].action, "new")

    def test_incremental_skips_synced_turns(self):
        """增量同步跳过已同步轮次"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
            Turn(turn_number=1, user_content="how?", assistant_content="like this"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        # 先全量同步
        engine.sync_session(source, session, incremental=False)
        # 再增量同步（应跳过）
        results = engine.sync_session(source, session, incremental=True)
        self.assertEqual(len(results), 0)

    def test_noise_turn_marked_noise(self):
        """噪音轮次标记为 noise"""
        engine = self._make_engine()
        source = FakeAgentSource()
        # 包含特殊噪音标记的内容
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="[SYSTEM_INIT]", assistant_content=""),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        with patch(
            "core.sync_framework.sync_engine_support.is_noise_message",
            return_value=True,
        ):
            results = engine.sync_session(source, session, incremental=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "noise")

    def test_duplicate_turn_skipped(self):
        """相同内容去重跳过"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        # 第一次同步
        engine.sync_session(source, session, incremental=False)
        # 第二次同步（相同内容）
        results = engine.sync_session(source, session, incremental=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "skipped")

    def test_canonical_session_id_dedupes_alias_paths(self):
        """同一会话被不同路径发现时按 canonical_session_id 去重。"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        sessions = [
            SessionInfo(
                session_id="alias-a",
                canonical_session_id="canonical-1",
                session_aliases=["alias-a"],
                source_path=Path("/tmp/a.jsonl"),
                mtime=1,
            ),
            SessionInfo(
                session_id="alias-b",
                canonical_session_id="canonical-1",
                session_aliases=["alias-b"],
                source_path=Path("/tmp/b.jsonl"),
                mtime=2,
            ),
        ]

        result = engine.sync_batch(source, sessions, incremental=False)

        self.assertEqual(result.total_sessions, 2)
        self.assertEqual(len(result.successful), 1)
        self.assertEqual(result.successful[0]["session_id"], "canonical-1")
        self.assertEqual(self.mock_backend.save.call_count, 1)
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT session_id, turn_number FROM sync_log"
            ).fetchall()
        self.assertEqual(rows, [("canonical-1", 0)])

    def test_raw_source_metadata_preserves_session_metadata(self):
        """SessionInfo metadata 会进入 raw store 来源元数据。"""
        engine = self._make_engine()
        source = FakeAgentSource()
        session = SessionInfo(
            session_id="sess-1",
            source_path=Path("/tmp/s.json"),
            metadata={
                "parent_session_id": "parent-1",
                "title": "rooted session",
            },
        )
        turn = Turn(
            turn_number=0,
            user_content="hi",
            assistant_content="hello",
            metadata={"title": "turn title"},
        )

        metadata = engine._raw_source_metadata(source, session, turn)

        self.assertEqual(metadata["parent_session_id"], "parent-1")
        self.assertEqual(metadata["title"], "turn title")
        self.assertEqual(metadata["source_session_id"], "sess-1")

    def test_session_start_end_hooks_called(self):
        """session_start 和 session_end hooks 被调用"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(
            session_id="sess-1", source_path=Path("/tmp/s.json"), working_dir="/proj"
        )
        engine.sync_session(source, session, incremental=False)
        self.assertEqual(len(source.session_start_calls), 1)
        self.assertEqual(source.session_start_calls[0][0], "sess-1")
        self.assertEqual(len(source.session_end_calls), 1)

    def test_long_content_uses_save(self):
        """超长内容使用 save 分片（避免重复字符触发噪音过滤）"""
        engine = self._make_engine()
        source = FakeAgentSource()
        # 使用多样化的长内容，避免 is_noise_message 的重复字符检测
        long_content = "\n".join(
            [f"Line {i}: discussion about Python asyncio patterns" for i in range(500)]
        )
        source.parse_turns = lambda _p: [
            Turn(
                turn_number=0,
                user_content=long_content,
                assistant_content="Here is the analysis...",
            ),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        self.assertTrue(self.mock_backend.save.called)

    def test_rate_limit_records_failure(self):
        """429 速率限制记录失败"""
        from core.sync_framework.storage_backend import StorageRateLimitError

        self.mock_backend.save.side_effect = StorageRateLimitError("too fast")
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        results = engine.sync_session(source, session, incremental=False)
        self.assertEqual(results[0].action, "failed")
        self.assertIn("rate_limit", results[0].error)

    def test_auth_error_records_failure(self):
        """401 认证失败记录失败"""
        from core.sync_framework.storage_backend import StorageAuthError

        self.mock_backend.save.side_effect = StorageAuthError("bad token")
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        results = engine.sync_session(source, session, incremental=False)
        self.assertEqual(results[0].action, "failed")
        self.assertIn("auth_error", results[0].error)

    def test_tags_contain_required_fields(self):
        """标签包含必需字段"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        tags = self._get_save_tags()
        tag_str = " ".join(tags)
        self.assertIn("status=raw", tag_str)
        self.assertIn("layer=L1", tag_str)
        self.assertIn("content_type=session-record", tag_str)

    def test_code_detection_tag(self):
        """包含代码时添加 has-code 标签"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="```python\nprint(1)\n```", assistant_content="ok"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        tags = self._get_save_tags()
        self.assertTrue(any("has-code" in t for t in tags))

    def test_structured_capture_visible_in_markdown(self):
        """工具调用、工具结果和 reasoning artifact 引用会进入后端可见内容"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(
                turn_number=0,
                user_content="please run tests",
                assistant_content="I ran them",
                tool_calls=[{"name": "pytest", "input": {"path": "tests"}}],
                tool_results=[{"stdout": "2 passed", "stderr": "", "tool_use_id": "tool-1"}],
                reasoning="internal reasoning should be artifacted",
            ),
        ]
        session = SessionInfo(session_id="sess-structured", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)

        content = self._get_save_content()
        self.assertIn("## Tool Calls", content)
        self.assertIn("pytest", content)
        self.assertIn("## Tool Results", content)
        self.assertIn("2 passed", content)
        self.assertIn("Reasoning captured", content)
        self.assertNotIn("internal reasoning should be artifacted", content)

        tags = self._get_save_tags()
        self.assertIn("has-tools=true", tags)
        self.assertIn("has-reasoning=true", tags)
        self.assertIn("reasoning_capture=artifact_summary", tags)

    def test_skip_distill_tag_for_wiki_content(self):
        """wiki 生成内容添加 skip-distill 标签"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(
                turn_number=0,
                user_content="<wiki-context>some ref</wiki-context>",
                assistant_content="ok",
            ),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        tags = self._get_save_tags()
        self.assertTrue(any("skip-distill" in t for t in tags))

    def test_record_synced_to_db(self):
        """同步记录写入数据库"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT agent_name, session_id, turn_number, status FROM sync_log WHERE agent_name = ?",  # noqa: E501
                (source.name,),
            )
            rows = cursor.fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "new")

    def test_get_synced_turns_counts_new_status(self):
        """缺洞审计应把当前成功状态 new 识别为已同步"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-audit", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        self.assertEqual(engine._get_synced_turns(source.name, session.session_id), [0])

    def test_get_synced_turns_counts_backfilled_status(self):
        """历史 backfilled 状态也应被缺洞审计识别为已同步。"""
        engine = self._make_engine()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (agent_name, session_id, turn_number, content_hash, status)
                VALUES (?, ?, ?, ?, ?)
            """,
                ("claude", "sess-backfilled", 7, "hash", "backfilled"),
            )
            conn.commit()
        self.assertEqual(engine._get_synced_turns("claude", "sess-backfilled"), [7])

    def test_sync_single_turn_can_skip_backend_duplicate_check(self):
        """历史缺洞回填可跳过后端全量兜底查重，避免每 turn 拉全库。"""
        engine = self._make_engine()
        source = FakeAgentSource()
        session = SessionInfo(session_id="sess-fast-backfill", source_path=Path("/tmp/s.json"))
        turn = Turn(turn_number=0, user_content="hi", assistant_content="hello")

        with patch.object(
            engine, "_check_backend_duplicate", side_effect=AssertionError("should not call")
        ):
            result = engine.sync_single_turn(
                source,
                session,
                turn,
                incremental=False,
                check_backend_duplicate=False,
            )

        self.assertEqual(result.action, "new")

    def test_sync_single_turn_uses_backend_duplicate_cache(self):
        """历史回填使用后端缓存兜底防重，不重复写入已有记录。"""
        engine = self._make_engine()
        source = FakeAgentSource()
        session = SessionInfo(session_id="sess-cache-backfill", source_path=Path("/tmp/s.json"))
        turn = Turn(turn_number=0, user_content="hi", assistant_content="hello")

        content = engine._sanitize_content(
            engine._build_markdown(turn, session.session_id, source.model_tag)
        )
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
        self.mock_backend.list_by_tags.return_value = [
            Mock(
                uid="existing-uid",
                content=content,
                tags=[
                    "source=claude",
                    "session=sess-cache-backfill",
                    "turn=1",
                    f"content_hash={content_hash}",
                ],
            )
        ]
        duplicate_cache = engine.build_backend_duplicate_cache(source.name)

        with patch.object(
            engine, "_check_backend_duplicate", side_effect=AssertionError("should not call")
        ):
            result = engine.sync_single_turn(
                source,
                session,
                turn,
                incremental=False,
                backend_duplicate_cache=duplicate_cache,
            )

        self.assertEqual(result.action, "skipped")
        self.assertEqual(result.backend_uids, ["existing-uid"])
        self.mock_backend.save.assert_not_called()

    def test_failed_sync_log_same_hash_is_not_treated_as_synced(self):
        """failed 记录不能因为 content_hash 相同就被当作已同步跳过。"""
        engine = self._make_engine()
        source = FakeAgentSource()
        session = SessionInfo(session_id="sess-failed-retry", source_path=Path("/tmp/s.json"))
        turn = Turn(turn_number=0, user_content="hi", assistant_content="hello")

        content = engine._sanitize_content(
            engine._build_markdown(turn, session.session_id, source.model_tag)
        )
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (agent_name, session_id, turn_number, content_hash, status)
                VALUES (?, ?, ?, ?, ?)
            """,
                (source.name, session.session_id, turn.turn_number, content_hash, "failed"),
            )
            conn.commit()

        self.mock_backend.list_by_tags.return_value = []
        result = engine.sync_single_turn(source, session, turn, incremental=False)

        self.assertEqual(result.action, "updated")
        self.mock_backend.save.assert_called_once()
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM sync_log WHERE session_id=? AND turn_number=?",
                (session.session_id, turn.turn_number),
            ).fetchone()
        self.assertEqual(row[0], "updated")

    def test_user_signals_collected(self):
        """画像信号采集到 user_signals 表"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi?", assistant_content="hello!"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT agent, session_id, user_questions FROM user_signals WHERE agent = ?",
                (source.name,),
            )
            rows = cursor.fetchall()
            self.assertEqual(len(rows), 1)
            # user_questions 应为 1（hi? 中有一个问号）
            self.assertEqual(rows[0][2], 1)


class TestSyncEngineBatchSync(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "sync_test.db"
        self.mock_backend = Mock()
        self.mock_backend._sanitize = lambda x: x  # noqa
        self.mock_backend.save.return_value = [Mock(uid="uid-1")]
        self.mock_backend.list_by_tags.return_value = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_engine(self):
        with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
            return SyncEngine(backend=self.mock_backend, db_path=str(self.db_path))

    def test_batch_sync_counts_stats(self):
        """批量同步统计成功/失败/跳过"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
            Turn(turn_number=1, user_content="how?", assistant_content="like this"),
        ]
        sessions = [
            SessionInfo(session_id="sess-a", source_path=Path("/tmp/a.json")),
            SessionInfo(session_id="sess-b", source_path=Path("/tmp/b.json")),
        ]
        result = engine.sync_batch(source, sessions, incremental=False)
        self.assertEqual(result.total_sessions, 2)
        self.assertEqual(len(result.successful), 2)
        self.assertEqual(result.turn_stats["new"], 4)

    def test_batch_sync_partial_failure(self):
        """批量同步部分 session 失败"""
        engine = self._make_engine()
        source = FakeAgentSource()
        call_count = [0]

        def parse_turns(path):
            call_count[0] += 1
            if call_count[0] == 1:
                return [Turn(turn_number=0, user_content="ok", assistant_content="ok")]
            raise RuntimeError("parse error")

        source.parse_turns = parse_turns
        sessions = [
            SessionInfo(session_id="sess-a", source_path=Path("/tmp/a.json")),
            SessionInfo(session_id="sess-b", source_path=Path("/tmp/b.json")),
        ]
        result = engine.sync_batch(source, sessions, incremental=False)
        self.assertEqual(len(result.successful), 1)
        self.assertEqual(len(result.failed), 1)


class TestSyncEngineRetryFailed(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "sync_test.db"
        self.mock_backend = Mock()
        self.mock_backend._sanitize = lambda x: x  # noqa
        self.mock_backend.save.return_value = [Mock(uid="uid-retry")]
        self.mock_backend.list_by_tags.return_value = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_engine(self):
        with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
            return SyncEngine(backend=self.mock_backend, db_path=str(self.db_path))

    def test_retries_failed_records(self):
        """重试失败的同步记录"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hello world", assistant_content="goodbye world")
        ]
        # 直接插入失败记录到数据库
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (
                    agent_name, session_id, turn_number,
                    content_hash, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                ("claude", "sess-1", 0, "abc", "failed", "server_error: timeout"),
            )
            conn.commit()

        with patch("core.sync_framework.registry.SourceRegistry.get", return_value=source):
            results = engine.retry_failed(limit=10)
        self.assertGreaterEqual(len(results), 1)

    def test_skips_auth_errors(self):
        """auth_error 不被重试"""
        engine = self._make_engine()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (
                    agent_name, session_id, turn_number,
                    content_hash, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                ("claude", "sess-1", 0, "abc", "failed", "auth_error: bad token"),
            )
            conn.commit()

        with patch(
            "core.sync_framework.registry.SourceRegistry.get", return_value=FakeAgentSource()
        ):
            results = engine.retry_failed(limit=10)
        self.assertEqual(len(results), 0)


class TestAgentRegistry(unittest.TestCase):
    def setUp(self):
        # 清理注册表状态
        AgentRegistry._registry.clear()
        AgentRegistry._instances.clear()

    def tearDown(self):
        AgentRegistry._registry.clear()
        AgentRegistry._instances.clear()

    def test_register_and_list(self):
        """注册后可在列表中看到"""
        AgentRegistry.register("claude", ClaudeSource)
        self.assertIn("claude", AgentRegistry.list_registered())

    def test_get_returns_instance(self):
        """get 返回已实例化的 AgentSource"""
        fake_path = Path(tempfile.gettempdir())
        with patch.object(
            ClaudeSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=fake_path,
        ):
            AgentRegistry.register("claude", ClaudeSource)
            source = AgentRegistry.get("claude")
        self.assertIsInstance(source, ClaudeSource)

    def test_get_returns_none_when_not_found(self):
        """未注册的 Agent 返回 None"""
        result = AgentRegistry.get("nonexistent")
        self.assertIsNone(result)

    def test_auto_discover_skips_missing_dirs(self):
        """数据目录不存在的 Agent 被跳过"""
        with (
            patch.object(ClaudeSource, "data_dir", new_callable=PropertyMock, return_value=None),
            patch.object(PathDiscover, "find", return_value=None),
        ):
            AgentRegistry.register("claude", ClaudeSource)
            discovered = AgentRegistry.auto_discover()
        self.assertEqual(len(discovered), 0)

    def test_register_builtin_agents(self):
        """注册内置 Agent（部分可能因模块不存在而跳过）"""
        AgentRegistry.register_builtin_agents()
        # 至少应有部分尝试注册
        registered = AgentRegistry.list_registered()
        self.assertIsInstance(registered, list)


class TestPathDiscover(unittest.TestCase):
    def setUp(self):
        PathDiscover.invalidate_cache()

    def test_user_config_never_follows_a_leaf_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_dir = root / "configs"
            config_dir.mkdir()
            outside = root / "outside.json"
            outside.write_text('{"claude": "/tmp/forged"}', encoding="utf-8")
            (config_dir / "agent_paths.json").symlink_to(outside)
            config = SimpleNamespace(data_dir=root)

            with patch(
                "core.sync_framework.registry.get_config",
                return_value=config,
            ):
                with self.assertRaises(PathDiscoveryUnavailableError):
                    PathDiscover._load_user_config("claude")  # noqa: SLF001

    def test_find_user_config_priority(self):
        """用户配置优先级最高"""
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "agent_paths.json"
            config_path.write_text(json.dumps({"claude": td}))
            with patch.object(PathDiscover, "_load_user_config", return_value={"claude": td}):
                result = PathDiscover.find("claude")
            self.assertEqual(str(result), td)

    def test_find_env_var(self):
        """环境变量第二优先级"""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"OPENCLAW_STATE_DIR": td}):
                result = PathDiscover.find("openclaw")
            self.assertEqual(str(result), td)

    def test_find_standard_path(self):
        """标准路径回退"""
        with tempfile.TemporaryDirectory() as td:
            claude_dir = Path(td) / ".claude"
            claude_dir.mkdir()
            # 修改 HOME/USERPROFILE 环境变量使 ~ 扩展到临时目录（跨平台）
            env_patch = {"HOME": td, "USERPROFILE": td}
            with patch.dict("os.environ", env_patch):
                result = PathDiscover.find("claude")
            self.assertEqual(result, claude_dir)

    def test_find_returns_none_when_not_found(self):
        """全部回退失败返回 None"""
        with (
            patch.object(PathDiscover, "_load_user_config", return_value={}),
            patch.object(PathDiscover, "_discover_from_process", return_value=None),
        ):
            result = PathDiscover.find("nonexistent-agent-xyz")
        self.assertIsNone(result)

    def test_heuristic_marker_invalid_utf8_is_unavailable_not_detected(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            candidate_dir = home / "claude-profile"
            candidate_dir.mkdir()
            (candidate_dir / "settings.json").write_bytes(
                b'{"hooks":true,\xff"other":false}'
            )
            resolver = {
                "heuristic": {
                    "filenames": ["settings.json"],
                    "content_marker": '"hooks"',
                }
            }
            with (
                patch.object(Path, "home", return_value=home),
                patch.object(PathDiscover, "_root_resolver", return_value=resolver),
            ):
                with self.assertRaises(PathDiscoveryUnavailableError):
                    PathDiscover._heuristic_search("claude")  # noqa: SLF001


class _ConcreteTrigger(BaseTrigger):
    """用于测试的具体触发器子类"""

    def start(self, watch_path: Path):
        pass

    def stop(self):
        pass


class TestBaseTrigger(unittest.TestCase):
    def test_backoff_delay_exponential(self):
        """指数退避延迟递增"""
        trigger = _ConcreteTrigger(callback=lambda x: None, source_name="test")
        delays = [trigger._backoff_delay() for _ in range(5)]
        for i in range(1, len(delays)):
            self.assertGreaterEqual(delays[i], delays[i - 1])
        # 最大不超过 300 秒
        self.assertLessEqual(delays[-1], 300)

    def test_execute_callback_resets_error_count(self):
        """回调成功后错误计数减少"""
        trigger = _ConcreteTrigger(callback=lambda x: None, source_name="test")
        trigger._error_count = 2
        trigger._execute_callback("/tmp/file")
        self.assertEqual(trigger._error_count, 1)

    def test_execute_callback_increments_error_count(self):
        """回调失败后错误计数增加"""

        def boom(_):
            raise RuntimeError("boom")

        trigger = _ConcreteTrigger(callback=boom, source_name="test")
        trigger._execute_callback("/tmp/file")
        self.assertEqual(trigger._error_count, 1)


@unittest.skipUnless(_WATCHDOG_AVAILABLE, "watchdog 未安装")
class TestWatchdogTrigger(unittest.TestCase):
    @patch("core.sync_framework.triggers.Observer")
    def test_start_stop_lifecycle(self, mock_observer_cls):
        """启动和停止生命周期"""
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer
        with tempfile.TemporaryDirectory() as td:
            trigger = WatchdogTrigger(callback=lambda x: None, source_name="test", debounce=0.1)
            trigger.start(Path(td))
            self.assertTrue(trigger._running)
            trigger.stop()
            self.assertFalse(trigger._running)
            mock_observer.stop.assert_called_once()

    @patch("core.sync_framework.triggers.Observer")
    def test_debounce_delays_callback(self, mock_observer_cls):
        """去抖动延迟回调执行"""
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer
        with tempfile.TemporaryDirectory() as td:
            calls = []
            trigger = WatchdogTrigger(
                callback=lambda p: calls.append(p), source_name="test", debounce=0.1
            )
            trigger.start(Path(td))
            trigger._on_event("/tmp/test.json")
            trigger._on_event("/tmp/test.json")
            time.sleep(0.3)
            trigger.stop()
            # 虽然触发两次，但去抖动后应只执行一次
            self.assertLessEqual(len(calls), 1)


class TestPollingTrigger(unittest.TestCase):
    def test_start_stop_lifecycle(self):
        """启动和停止生命周期"""
        with tempfile.TemporaryDirectory() as td:
            calls = []
            trigger = PollingTrigger(
                callback=lambda p: calls.append(p), source_name="test", interval=0.1
            )
            trigger.start(Path(td))
            self.assertTrue(trigger._running)
            time.sleep(0.3)
            trigger.stop()
            self.assertFalse(trigger._running)

    def test_detects_new_files(self):
        """检测到新文件触发回调"""
        with tempfile.TemporaryDirectory() as td:
            calls = []
            trigger = PollingTrigger(
                callback=lambda p: calls.append(p), source_name="test", interval=0.1
            )
            trigger.start(Path(td))
            # 创建新文件
            test_file = Path(td) / "test.txt"
            test_file.write_text("hello")
            time.sleep(0.3)
            trigger.stop()
            self.assertGreaterEqual(len(calls), 1)
            self.assertIn(str(test_file), calls)

    def test_state_persistence(self):
        """轮询状态持久化到数据库"""
        with tempfile.TemporaryDirectory() as td:
            trigger = PollingTrigger(callback=lambda p: None, source_name="test", interval=0.1)
            trigger.start(Path(td))
            test_file = Path(td) / "test.txt"
            test_file.write_text("hello")
            time.sleep(0.3)
            trigger.stop()
            # 状态已保存到数据库
            self.assertTrue(trigger._db_path.exists())


class TestFileIngestor(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_ingestor(self):
        ingestor = FileIngestor(
            receipt_factory=lambda **kwargs: {
                "success": True,
                "status": "queued",
                "source_event_id": "raw-event-1",
                "raw_event_id": "raw-event-1",
                "provenance_id": "raw-event-1",
                "capture_result": {
                    "status": "queued",
                    "capture_dedupe_key": "capture-file",
                },
            },
        )
        return ingestor

    def test_ingest_txt_file(self):
        """摄入 txt 文件"""
        ingestor = self._make_ingestor()
        test_file = Path(self.tmpdir.name) / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")
        result = ingestor.ingest_file(test_file, agent_name="file")
        self.assertIsNotNone(result)
        self.assertIn("file-ext=txt", result[0].tags)
        self.assertEqual(result[0].metadata["canonical_owner"], "raw_event_store")

    def test_ingest_missing_file_returns_none(self):
        """文件不存在返回 None"""
        ingestor = self._make_ingestor()
        result = ingestor.ingest_file(Path("/nonexistent/file.txt"))
        self.assertIsNone(result)

    def test_ingest_oversized_file_returns_none(self):
        """超大文件返回 None"""
        ingestor = self._make_ingestor()
        test_file = Path(self.tmpdir.name) / "big.bin"
        test_file.write_bytes(b"x" * (11 * 1024 * 1024))  # 11MB
        result = ingestor.ingest_file(test_file)
        self.assertIsNone(result)

    def test_ingest_directory(self):
        """批量摄入目录"""
        ingestor = self._make_ingestor()
        subdir = Path(self.tmpdir.name) / "docs"
        subdir.mkdir()
        (subdir / "a.txt").write_text("a")
        (subdir / "b.txt").write_text("b")
        (subdir / "c.pdf").write_bytes(b"%PDF")  # 不支持，但会尝试
        count = ingestor.ingest_directory(subdir, agent_name="file")
        self.assertGreaterEqual(count, 2)

    def test_extract_plain_encoding_fallback(self):
        """纯文本编码回退"""
        ingestor = self._make_ingestor()
        test_file = Path(self.tmpdir.name) / "gbk.txt"
        test_file.write_bytes("中文内容".encode("gbk"))
        text = ingestor._extract_text(test_file)
        self.assertEqual(text, "中文内容")

    def test_is_supported_filters_extensions(self):
        """_is_supported 正确过滤扩展名"""
        ingestor = self._make_ingestor()
        self.assertTrue(ingestor._is_supported(Path("a.txt")))
        self.assertTrue(ingestor._is_supported(Path("a.pdf")))
        self.assertTrue(ingestor._is_supported(Path("a.docx")))
        self.assertFalse(ingestor._is_supported(Path("a.exe")))

    def test_build_file_markdown(self):
        """构建 Markdown 包含文件元数据"""
        ingestor = self._make_ingestor()
        test_file = Path(self.tmpdir.name) / "doc.txt"
        test_file.write_text("content")
        md = ingestor._build_file_markdown(test_file, "file text")
        self.assertIn("File: doc.txt", md)
        self.assertIn("file text", md)


if __name__ == "__main__":
    unittest.main()


class TestTriggerDispatcher(unittest.TestCase):
    """验证 TriggerDispatcher 根据策略创建正确的触发器类型（P1-#19）"""

    def test_register_watchdog_creates_watchdog_trigger(self):
        from core.sync_framework.triggers import TriggerDispatcher

        with tempfile.TemporaryDirectory() as td:
            with patch("core.sync_framework.triggers.WatchdogTrigger") as mock_cls:
                mock_instance = MagicMock()
                mock_cls.return_value = mock_instance
                dispatcher = TriggerDispatcher(callback=lambda x: None)
                dispatcher.register(
                    "claude",
                    {"type": "watchdog", "events": ["modified"], "debounce": 2.0},
                    Path(td),
                )
                dispatcher.start_all()
                mock_cls.assert_called_once()
                mock_instance.start.assert_called_once_with(Path(td))

    def test_register_polling_creates_polling_trigger(self):
        from core.sync_framework.triggers import TriggerDispatcher

        with tempfile.TemporaryDirectory() as td:
            with patch("core.sync_framework.triggers.PollingTrigger") as mock_cls:
                mock_instance = MagicMock()
                mock_cls.return_value = mock_instance
                dispatcher = TriggerDispatcher(callback=lambda x: None)
                dispatcher.register(
                    "openclaw",
                    {"type": "polling", "interval": 3600, "pattern": "*.txt"},
                    Path(td),
                )
                dispatcher.start_all()
                mock_cls.assert_called_once()
                mock_instance.start.assert_called_once_with(Path(td))

    def test_register_hybrid_creates_hybrid_trigger(self):
        from core.sync_framework.triggers import TriggerDispatcher

        with tempfile.TemporaryDirectory() as td:
            with patch("core.sync_framework.triggers.HybridTrigger") as mock_cls:
                mock_instance = MagicMock()
                mock_cls.return_value = mock_instance
                dispatcher = TriggerDispatcher(callback=lambda x: None)
                dispatcher.register(
                    "kimi",
                    {
                        "type": "hybrid",
                        "events": ["modified", "created"],
                        "debounce": 1.0,
                        "interval": 600,
                    },
                    Path(td),
                )
                dispatcher.start_all()
                mock_cls.assert_called_once()
                mock_instance.start.assert_called_once_with(Path(td))

    def test_unknown_strategy_is_ignored(self):
        from core.sync_framework.triggers import TriggerDispatcher

        with tempfile.TemporaryDirectory() as td:
            with patch("core.sync_framework.triggers.WatchdogTrigger") as mock_cls:
                dispatcher = TriggerDispatcher(callback=lambda x: None)
                dispatcher.register("x", {"type": "unknown"}, Path(td))
                dispatcher.start_all()
                mock_cls.assert_not_called()

    def test_start_all_and_stop_all(self):
        from core.sync_framework.triggers import TriggerDispatcher

        with tempfile.TemporaryDirectory() as td:
            with patch("core.sync_framework.triggers.WatchdogTrigger") as mock_cls:
                mock_instance = MagicMock()
                mock_cls.return_value = mock_instance
                dispatcher = TriggerDispatcher(callback=lambda x: None)
                dispatcher.register("s1", {"type": "watchdog"}, Path(td))
                dispatcher.register("s2", {"type": "watchdog"}, Path(td))
                dispatcher.start_all()
                dispatcher.stop_all()
                self.assertEqual(mock_instance.start.call_count, 2)
                self.assertEqual(mock_instance.stop.call_count, 2)


class TestHybridTrigger(unittest.TestCase):
    """验证 HybridTrigger 启动/停止会同时操作 watchdog 与 polling 子触发器"""

    @patch("core.sync_framework.triggers.WatchdogTrigger")
    @patch("core.sync_framework.triggers.PollingTrigger")
    def test_start_stop_delegates_to_sub_triggers(self, mock_polling, mock_watchdog):
        from core.sync_framework.triggers import HybridTrigger

        wd_instance = MagicMock()
        poll_instance = MagicMock()
        mock_watchdog.return_value = wd_instance
        mock_polling.return_value = poll_instance

        trigger = HybridTrigger(callback=lambda x: None, source_name="kimi")
        trigger.start(Path("/fake/kimi"))
        trigger.stop()

        mock_watchdog.assert_called_once()
        mock_polling.assert_called_once()
        wd_instance.start.assert_called_once_with(Path("/fake/kimi"))
        poll_instance.start.assert_called_once_with(Path("/fake/kimi"))
        wd_instance.stop.assert_called_once()
        poll_instance.stop.assert_called_once()


class TestSyncEventBusOwnership(unittest.TestCase):
    """Sync polling events must never escape the caller-owned data target."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        from core.mnemos_bus import reset_event_bus

        reset_event_bus()

    def tearDown(self):
        from core.mnemos_bus import reset_event_bus

        reset_event_bus()
        self.tmpdir.cleanup()

    @staticmethod
    def _session() -> SessionInfo:
        return SessionInfo(
            session_id="event-target",
            source_path=Path("/tmp/event-target.json"),
        )

    def _engine(self, target: Path) -> SyncEngine:
        config = {
            **_FAKE_CONFIG,
            "data_dir": self.root / "unrelated-default",
        }
        with patch(
            "core.sync_framework.sync_engine.get_config",
            return_value=config,
        ):
            return SyncEngine(
                backend=Mock(),
                db_path=str(target / "sync_log.db"),
            )

    def test_explicit_sync_directory_owns_the_polled_event_database(self):
        import core.mnemos_bus as bus_module

        target = self.root / "caller-owned"
        engine = self._engine(target)
        try:
            with patch(
                "core.mnemos_bus._should_force_transient_pool",
                return_value=False,
            ), patch(
                "core.mnemos_bus.publish_event",
                side_effect=_REAL_PUBLISH_EVENT,
            ):
                engine.sync_session(
                    FakeAgentSource(),
                    self._session(),
                )
        finally:
            engine.close()

        bus = bus_module._global_bus
        self.assertIsNotNone(bus)
        self.assertEqual(engine.raw_store.db_path, target / "raw_events.db")
        self.assertEqual(bus._db_path, target / "events.db")
        rows = bus._get_conn().execute(
            "SELECT event_type, source FROM events"
        ).fetchall()
        rows = [(row[0], row[1]) for row in rows]
        self.assertEqual(rows, [("polled", "claude")])
        self.assertNotEqual(
            bus._db_path,
            self.root / "unrelated-default" / "events.db",
        )
        self.assertFalse(
            (self.root / "unrelated-default" / "raw_events.db").exists()
        )

    def test_foreign_process_bus_conflict_fails_before_any_polled_write(self):
        from core.mnemos_bus import get_event_bus

        class EventConfig:
            def __init__(self, database_dir: Path):
                self.database_dir = database_dir
                self.mnemos_dir = database_dir
                self.data_dir = database_dir

            def get(self, _key, default=None):
                return default

        foreign_dir = self.root / "foreign"
        with patch(
            "core.mnemos_bus._should_force_transient_pool",
            return_value=False,
        ), patch(
            "core.mnemos_bus.publish_event",
            side_effect=_REAL_PUBLISH_EVENT,
        ):
            foreign_bus = get_event_bus(config=EventConfig(foreign_dir))
            self.assertEqual(foreign_bus._db_path, foreign_dir / "events.db")
            target = self.root / "caller-owned"
            engine = self._engine(target)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "different durable event database",
                ):
                    engine.sync_session(
                        FakeAgentSource(),
                        self._session(),
                    )
            finally:
                engine.close()

        event_count = foreign_bus._get_conn().execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_mapping_config_uses_one_database_owner_for_raw_and_event(self):
        import core.mnemos_bus as bus_module

        configured_dir = self.root / "mapping-owned"
        config = {
            **_FAKE_CONFIG,
            "database_dir": configured_dir,
            "data_dir": self.root / "mapping-unrelated",
        }
        engine = SyncEngine(
            backend=Mock(),
            db_path=str(self.root / "explicit-sync" / "sync_log.db"),
            config=config,
        )
        try:
            self.assertEqual(
                engine.raw_store.db_path,
                configured_dir / "raw_events.db",
            )
            with patch(
                "core.mnemos_bus._should_force_transient_pool",
                return_value=False,
            ), patch(
                "core.mnemos_bus.publish_event",
                side_effect=_REAL_PUBLISH_EVENT,
            ):
                engine.sync_session(
                    FakeAgentSource(),
                    self._session(),
                )
        finally:
            engine.close()

        bus = bus_module._global_bus
        self.assertIsNotNone(bus)
        self.assertEqual(bus._db_path, configured_dir / "events.db")
