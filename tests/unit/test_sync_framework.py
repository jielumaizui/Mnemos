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

import sys
import json
import time
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

_FAKE_CONFIG = {
    "memos_token": "fake-token",
    "memos_api_url": "http://localhost:5230",
    "data_dir": Path(tempfile.gettempdir()) / "mnemos_test",
    "get": lambda key, default=None: {
        "memos.max_content_bytes": 7792,
        "memos.ingest_batch_size": 10,
        "memos.ingest_batch_interval": 0,
        "memos.query_cache_ttl": 30,
    }.get(key, default),
}

with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG), \
     patch("core.sync_framework.triggers.get_config", return_value=_FAKE_CONFIG), \
     patch("core.sync_framework.file_ingestor.get_config", return_value=_FAKE_CONFIG):
    from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn, SyncResult
    from core.sync_framework.sync_engine import SyncEngine
    from core.sync_framework.registry import AgentRegistry, PathDiscover
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

    def test_turn_defaults(self):
        """Turn 默认值正确"""
        t = Turn(turn_number=0, user_content="hi", assistant_content="hello")
        self.assertEqual(t.metadata, {})
        self.assertIsNone(t.timestamp)

    def test_sync_result_defaults(self):
        """SyncResult 默认值正确"""
        r = SyncResult(session_id="s1", turn_number=0, action="new")
        self.assertEqual(r.memos_uids, [])
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
        mock_client = Mock()
        engine = SyncEngine(client=mock_client, db_path=str(self.db_path))
        self.assertTrue(self.db_path.exists())
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cursor.fetchall()}
            self.assertIn("sync_log", tables)
            self.assertIn("user_signals", tables)

    @patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG)
    def test_uses_provided_client(self, _mock_cfg):
        """使用传入的 MemosClient"""
        mock_client = Mock()
        engine = SyncEngine(client=mock_client, db_path=str(self.db_path))
        self.assertIs(engine.client, mock_client)

    @patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG)
    def test_shard_threshold_from_config(self, _mock_cfg):
        """分片阈值从 Config 读取"""
        mock_client = Mock()
        engine = SyncEngine(client=mock_client, db_path=str(self.db_path))
        self.assertEqual(engine._shard_threshold, 7792)


class TestSyncEngineSessionSync(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "sync_test.db"
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-1")
        self.mock_client.save_long_content.return_value = [Mock(uid="uid-long")]

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_engine(self):
        with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
            return SyncEngine(client=self.mock_client, db_path=str(self.db_path))

    def _get_save_tags(self):
        """辅助：从 mock save 的位置参数中提取 tags"""
        call = self.mock_client.save.call_args
        if call and call[0] and len(call[0]) >= 2:
            return call[0][1]
        return call[1].get("tags", []) if call and call[1] else []

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
        with patch("core.sync_framework.sync_engine.is_noise_message", return_value=True):
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

    def test_session_start_end_hooks_called(self):
        """session_start 和 session_end hooks 被调用"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="hi", assistant_content="hello"),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"), working_dir="/proj")
        engine.sync_session(source, session, incremental=False)
        self.assertEqual(len(source.session_start_calls), 1)
        self.assertEqual(source.session_start_calls[0][0], "sess-1")
        self.assertEqual(len(source.session_end_calls), 1)

    def test_long_content_uses_save_long_content(self):
        """超长内容使用 save_long_content 分片（避免重复字符触发噪音过滤）"""
        engine = self._make_engine()
        source = FakeAgentSource()
        # 使用多样化的长内容，避免 is_noise_message 的重复字符检测
        long_content = "\n".join([f"Line {i}: discussion about Python asyncio patterns" for i in range(500)])
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content=long_content, assistant_content="Here is the analysis..."),
        ]
        session = SessionInfo(session_id="sess-1", source_path=Path("/tmp/s.json"))
        engine.sync_session(source, session, incremental=False)
        self.assertTrue(self.mock_client.save_long_content.called)

    def test_rate_limit_records_failure(self):
        """429 速率限制记录失败"""
        from integrations.styx import MemosRateLimitError
        self.mock_client.save.side_effect = MemosRateLimitError("too fast")
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
        from integrations.styx import MemosAuthError
        self.mock_client.save.side_effect = MemosAuthError("bad token")
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

    def test_skip_distill_tag_for_wiki_content(self):
        """wiki 生成内容添加 skip-distill 标签"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [
            Turn(turn_number=0, user_content="<wiki-context>some ref</wiki-context>", assistant_content="ok"),
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
                "SELECT agent_name, session_id, turn_number, status FROM sync_log WHERE agent_name = ?",
                (source.name,),
            )
            rows = cursor.fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "new")

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
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-1")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_engine(self):
        with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
            return SyncEngine(client=self.mock_client, db_path=str(self.db_path))

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
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-retry")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_engine(self):
        with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
            return SyncEngine(client=self.mock_client, db_path=str(self.db_path))

    def test_retries_failed_records(self):
        """重试失败的同步记录"""
        engine = self._make_engine()
        source = FakeAgentSource()
        source.parse_turns = lambda _p: [Turn(turn_number=0, user_content="hello world", assistant_content="goodbye world")]
        # 直接插入失败记录到数据库
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO sync_log (agent_name, session_id, turn_number, content_hash, status, error)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ("claude", "sess-1", 0, "abc", "failed", "server_error: timeout"))
            conn.commit()

        with patch("core.sync_framework.registry.AgentRegistry.get", return_value=source):
            results = engine.retry_failed(limit=10)
        self.assertGreaterEqual(len(results), 1)

    def test_skips_auth_errors(self):
        """auth_error 不被重试"""
        engine = self._make_engine()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO sync_log (agent_name, session_id, turn_number, content_hash, status, error)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ("claude", "sess-1", 0, "abc", "failed", "auth_error: bad token"))
            conn.commit()

        with patch("core.sync_framework.registry.AgentRegistry.get", return_value=FakeAgentSource()):
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
        AgentRegistry.register("fake", FakeAgentSource)
        self.assertIn("fake", AgentRegistry.list_registered())

    def test_get_returns_instance(self):
        """get 返回已实例化的 AgentSource"""
        AgentRegistry.register("fake", FakeAgentSource)
        fake_path = Path(tempfile.gettempdir())
        with patch.object(PathDiscover, "find", return_value=fake_path):
            source = AgentRegistry.get("fake")
        self.assertIsInstance(source, FakeAgentSource)

    def test_get_returns_none_when_not_found(self):
        """未注册的 Agent 返回 None"""
        result = AgentRegistry.get("nonexistent")
        self.assertIsNone(result)

    def test_auto_discover_skips_missing_dirs(self):
        """数据目录不存在的 Agent 被跳过"""
        AgentRegistry.register("fake", FakeAgentSource)
        with patch.object(PathDiscover, "find", return_value=None):
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
        with patch.object(PathDiscover, "_load_user_config", return_value={}), \
             patch.object(PathDiscover, "_discover_from_process", return_value=None):
            result = PathDiscover.find("nonexistent-agent-xyz")
        self.assertIsNone(result)


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
    def test_start_stop_lifecycle(self):
        """启动和停止生命周期"""
        with tempfile.TemporaryDirectory() as td:
            trigger = WatchdogTrigger(callback=lambda x: None, source_name="test", debounce=0.1)
            trigger.start(Path(td))
            self.assertTrue(trigger._running)
            trigger.stop()
            self.assertFalse(trigger._running)

    def test_debounce_delays_callback(self):
        """去抖动延迟回调执行"""
        with tempfile.TemporaryDirectory() as td:
            calls = []
            trigger = WatchdogTrigger(callback=lambda p: calls.append(p), source_name="test", debounce=0.1)
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
            trigger = PollingTrigger(callback=lambda p: calls.append(p), source_name="test", interval=0.1)
            trigger.start(Path(td))
            self.assertTrue(trigger._running)
            time.sleep(0.3)
            trigger.stop()
            self.assertFalse(trigger._running)

    def test_detects_new_files(self):
        """检测到新文件触发回调"""
        with tempfile.TemporaryDirectory() as td:
            calls = []
            trigger = PollingTrigger(callback=lambda p: calls.append(p), source_name="test", interval=0.1)
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
        self.mock_client = Mock()
        self.mock_client.save_long_content.return_value = [Mock(uid="uid-file")]

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_ingestor(self):
        with patch("core.sync_framework.file_ingestor.get_config", return_value=_FAKE_CONFIG):
            ingestor = FileIngestor(client=self.mock_client)
            return ingestor

    def test_ingest_txt_file(self):
        """摄入 txt 文件"""
        ingestor = self._make_ingestor()
        test_file = Path(self.tmpdir.name) / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")
        result = ingestor.ingest_file(test_file, agent_name="file")
        self.assertIsNotNone(result)
        self.mock_client.save_long_content.assert_called_once()
        call_kwargs = self.mock_client.save_long_content.call_args[1]
        self.assertIn("file-ext=txt", call_kwargs["tags"])

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
