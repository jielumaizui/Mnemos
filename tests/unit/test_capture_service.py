"""
CaptureService / CaptureQueue / CaptureWorkerPool 单元测试

覆盖项：
- capture_turn 去重（同 payload 10 次只入队一次）
- capture_turn 内容变化后允许重新入队
- capture_turn 队列积压时仍快速返回
- Worker 对 10KB+ 内容自动分片（调用 save_long_content）
- Worker 隔离来源失败（Codex 失败不影响 Claude）
- Worker 保持 session turn 顺序
- daemon 重启后 pending 队列恢复
- sync_log 反查 source/session/turn → memos_uids
"""

import sys
import json
import time
import sqlite3
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest

class _FakeConfig:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir or Path(tempfile.gettempdir()) / "mnemos_test_capture"
        self.memos_token = "fake-token"
        self.memos_api_url = "http://localhost:5230"
        self._values = {
            "memos.max_content_bytes": 7792,
            "memos.ingest_batch_size": 10,
            "memos.ingest_batch_interval": 0,
            "memos.query_cache_ttl": 30,
            "capture.max_queue_depth": 10000,
            "capture.per_source_max_queue_depth": 1000,
            "capture.max_workers": 2,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 1,
            "capture.max_payload_bytes": 200000,
            "capture.duplicate_ttl_days": 30,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


_FAKE_CONFIG = _FakeConfig()

with patch("core.sync_framework.capture_service.get_config", return_value=_FAKE_CONFIG), \
     patch("core.sync_framework.capture_queue.get_config", return_value=_FAKE_CONFIG), \
     patch("core.sync_framework.capture_worker.get_config", return_value=_FAKE_CONFIG), \
     patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
    from core.sync_framework.capture_service import CaptureService
    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_worker import CaptureWorkerPool


class TestCaptureQueue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.queue = CaptureQueue(db_path=str(self.db_path))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_enqueue_and_dequeue(self):
        """入队后出队能取到"""
        status = self.queue.enqueue(
            dedupe_key="key1",
            source_agent="codex",
            session_id="sess-1",
            turn_id="t1",
            turn_number=0,
            payload={"user_content": "hi"},
            content_hash="abc123",
        )
        self.assertEqual(status, "queued")

        events = self.queue.dequeue(limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_agent"], "codex")
        # dequeue 返回的是出队前的原始数据，数据库中已更新为 processing
        # 验证数据库状态
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM capture_events WHERE id = ?", (events[0]["id"],))
            row = cursor.fetchone()
            self.assertEqual(row[0], "processing")

    def test_duplicate_key_rejected(self):
        """相同 dedupe_key 第二次入队返回 duplicate"""
        self.queue.enqueue(
            dedupe_key="key-dup",
            source_agent="codex",
            session_id="sess-1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="abc",
        )
        status = self.queue.enqueue(
            dedupe_key="key-dup",
            source_agent="codex",
            session_id="sess-1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="abc",
        )
        self.assertEqual(status, "duplicate")

    def test_backpressure_when_full(self):
        """队列满时返回 backpressure"""
        fake_config_small = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_config_small._values["capture.max_queue_depth"] = 2
        with patch("core.sync_framework.capture_queue.get_config", return_value=fake_config_small):
            q2 = CaptureQueue(db_path=str(self.db_path))
            q2.enqueue("k1", "codex", "s1", None, 0, {}, "h1")
            q2.enqueue("k2", "codex", "s1", None, 1, {}, "h2")
            status = q2.enqueue("k3", "codex", "s1", None, 2, {}, "h3")
            self.assertEqual(status, "backpressure")

    def test_pending_count(self):
        """pending 统计正确"""
        self.queue.enqueue("k1", "codex", "s1", None, 0, {}, "h1")
        self.queue.enqueue("k2", "claude", "s2", None, 0, {}, "h2")
        self.assertEqual(self.queue.get_pending_count(), 2)
        self.assertEqual(self.queue.get_pending_count("codex"), 1)

    def test_daemon_restart_recovery(self):
        """daemon 重启后 pending 队列可恢复"""
        self.queue.enqueue("k1", "codex", "s1", None, 0, {}, "h1")
        self.queue.enqueue("k2", "codex", "s1", None, 1, {}, "h2")

        # 模拟重启：创建新的 CaptureQueue 实例，指向同一个 db
        q2 = CaptureQueue(db_path=str(self.db_path))
        self.assertEqual(q2.get_pending_count(), 2)
        events = q2.dequeue(limit=10)
        self.assertEqual(len(events), 2)

    def test_reset_processing_to_pending(self):
        """崩溃恢复：processing 状态回退到 pending"""
        self.queue.enqueue("k1", "codex", "s1", None, 0, {}, "h1")
        # 模拟出队后崩溃（状态变成 processing）
        self.queue.dequeue(limit=10)
        self.assertEqual(self.queue.get_pending_count(), 0)

        # 模拟重启后恢复
        reset_count = self.queue.reset_processing_to_pending()
        self.assertEqual(reset_count, 1)
        self.assertEqual(self.queue.get_pending_count(), 1)

    def test_dequeue_by_session(self):
        """按 session 过滤出队"""
        self.queue.enqueue("k1", "codex", "s1", None, 0, {}, "h1")
        self.queue.enqueue("k2", "codex", "s2", None, 0, {}, "h2")
        self.queue.enqueue("k3", "claude", "s1", None, 0, {}, "h3")

        events = self.queue.dequeue_by_session("codex", "s1", limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["session_id"], "s1")

    def test_backoff_state_persistence(self):
        """退避状态持久化到数据库"""
        self.queue.set_backoff_state("codex", 3, "2024-01-01T00:00:00")
        state = self.queue.get_backoff_state("codex")
        self.assertEqual(state["error_count"], 3)
        self.assertEqual(state["last_retry_at"], "2024-01-01T00:00:00")

        self.queue.clear_backoff_state("codex")
        state = self.queue.get_backoff_state("codex")
        self.assertEqual(state["error_count"], 0)

    def test_cleanup_old(self):
        """清理旧记录"""
        self.queue.enqueue("k1", "codex", "s1", None, 0, {}, "h1")
        # 标记为 done
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("UPDATE capture_events SET status = 'done', created_at = '2000-01-01' WHERE dedupe_key = 'k1'")
            conn.commit()
        self.queue.cleanup_old(days=1)
        self.assertEqual(self.queue.get_pending_count(), 0)


class TestCaptureServiceDedup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = CaptureQueue(db_path=str(self.db_path))
        # 确保 data_dir 存在
        _FAKE_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
        # 重置单例
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        # 停止可能启动的 worker，防止线程泄漏
        if CaptureService._instance and CaptureService._instance.worker_pool:
            try:
                CaptureService._instance.worker_pool.stop()
            except Exception:
                pass
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def _make_service(self):
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue)
            return service

    def test_capture_turn_dedup_same_payload(self):
        """同一条 turn 连续上报 10 次，只入队一次"""
        service = self._make_service()
        results = []
        for _ in range(10):
            r = service.capture_turn(
                source_agent="codex",
                session_id="sess-123",
                turn_id="turn-1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi there",
            )
            results.append(r)

        queued_count = sum(1 for r in results if r["status"] == "queued")
        dup_count = sum(1 for r in results if r["status"] == "duplicate")
        self.assertEqual(queued_count, 1)
        self.assertEqual(dup_count, 9)

    def test_capture_turn_allows_updated_hash(self):
        """内容变化后允许重新入队"""
        service = self._make_service()
        r1 = service.capture_turn(
            source_agent="codex",
            session_id="sess-123",
            turn_id="turn-1",
            turn_number=0,
            user_content="hello",
            assistant_content="hi there",
        )
        self.assertEqual(r1["status"], "queued")

        r2 = service.capture_turn(
            source_agent="codex",
            session_id="sess-123",
            turn_id="turn-1",
            turn_number=0,
            user_content="hello world",
            assistant_content="hi there",
        )
        self.assertEqual(r2["status"], "queued")

    def test_capture_turn_returns_fast_when_queue_backlogged(self):
        """队列积压时仍 < 200ms"""
        service = self._make_service()
        # 先积压一些
        for i in range(50):
            service.capture_turn(
                source_agent="codex",
                session_id="sess-bulk",
                turn_id=f"t{i}",
                turn_number=i,
                user_content=f"msg {i}",
                assistant_content="ok",
            )

        start = time.time()
        result = service.capture_turn(
            source_agent="codex",
            session_id="sess-bulk",
            turn_id="t-last",
            turn_number=999,
            user_content="last",
            assistant_content="ok",
        )
        elapsed_ms = (time.time() - start) * 1000
        self.assertLess(elapsed_ms, 200, f"MCP 返回太慢: {elapsed_ms:.1f}ms")
        self.assertIn(result["status"], ("queued", "duplicate", "backpressure"))

    def test_dedupe_includes_source_session_turn(self):
        """去重包含 source_agent + session_id + turn_id"""
        service = self._make_service()
        # 相同内容，不同 source
        r1 = service.capture_turn(
            source_agent="codex", session_id="s1", turn_id="t1",
            turn_number=0, user_content="hello", assistant_content="hi",
        )
        r2 = service.capture_turn(
            source_agent="claude", session_id="s1", turn_id="t1",
            turn_number=0, user_content="hello", assistant_content="hi",
        )
        self.assertEqual(r1["status"], "queued")
        self.assertEqual(r2["status"], "queued")

        # 相同内容，不同 session
        r3 = service.capture_turn(
            source_agent="codex", session_id="s2", turn_id="t1",
            turn_number=0, user_content="hello", assistant_content="hi",
        )
        self.assertEqual(r3["status"], "queued")


class TestCaptureWorker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = CaptureQueue(db_path=str(self.db_path))
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-1")
        self.mock_client.save_long_content.return_value = [
            Mock(uid="uid-long-1"), Mock(uid="uid-long-2")
        ]
        _FAKE_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_engine(self):
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            from core.sync_framework.sync_engine import SyncEngine
            return SyncEngine(client=self.mock_client, db_path=str(self.sync_db_path))

    def _make_worker(self):
        engine = self._make_engine()
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            pool = CaptureWorkerPool(queue=self.queue, sync_engine=engine)
            return pool

    def test_worker_uses_save_long_content_for_large_payload(self):
        """10KB+ 内容自动分片，调用 save_long_content"""
        worker = self._make_worker()
        long_text = "x" * 15000  # 超过 7792 字节阈值
        self.queue.enqueue(
            dedupe_key="k-large",
            source_agent="codex",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            payload={
                "user_content": long_text,
                "assistant_content": "response",
            },
            content_hash="hash-large",
        )

        # 手动处理一个批次
        worker._process_one_batch()
        self.mock_client.save_long_content.assert_called_once()

    def test_worker_isolates_source_failures(self):
        """Codex 失败不影响 Claude"""
        worker = self._make_worker()
        # 让 codex 的 save 抛异常
        def side_effect(content, tags, visibility, **kwargs):
            if "agent=codex" in str(tags):
                raise RuntimeError("codex boom")
            return Mock(uid="uid-ok")
        self.mock_client.save.side_effect = side_effect

        self.queue.enqueue("k1", "codex", "s1", None, 0, {"user_content": "hi", "assistant_content": "hello"}, "h1")
        self.queue.enqueue("k2", "claude", "s2", None, 0, {"user_content": "hi", "assistant_content": "hello"}, "h2")

        worker._process_one_batch()

        # claude 应该成功写入 sync_log（状态为 done）
        # codex 应该标记为 pending（因为会重试）
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT source_agent, status FROM capture_events")
            rows = {r[0]: r[1] for r in cursor.fetchall()}
            # codex 失败后会回退到 pending，claude 成功会标记为 done
            self.assertEqual(rows.get("claude"), "done")

    def test_worker_preserves_session_turn_order(self):
        """同一 session 按 turn_number 顺序处理"""
        worker = self._make_worker()
        processed = []
        original_save = self.mock_client.save

        def tracking_save(content, tags, visibility, **kwargs):
            # 从 content 中提取 turn 信息
            processed.append(content)
            return Mock(uid="uid")
        self.mock_client.save = tracking_save

        self.queue.enqueue("k3", "codex", "s1", None, 2, {"user_content": "msg3", "assistant_content": "ok"}, "h3")
        self.queue.enqueue("k1", "codex", "s1", None, 0, {"user_content": "msg1", "assistant_content": "ok"}, "h1")
        self.queue.enqueue("k2", "codex", "s1", None, 1, {"user_content": "msg2", "assistant_content": "ok"}, "h2")

        worker._process_one_batch()

        # 应该按 turn_number 0, 1, 2 顺序处理
        self.assertIn("msg1", processed[0])
        self.assertIn("msg2", processed[1])
        self.assertIn("msg3", processed[2])

    def test_flush_session_immediate(self):
        """end_session 触发 flush_session 立即处理指定 session"""
        worker = self._make_worker()
        self.queue.enqueue("k1", "codex", "s-flush", None, 0, {"user_content": "hi", "assistant_content": "hello"}, "h1")
        self.queue.enqueue("k2", "codex", "s-flush", None, 1, {"user_content": "bye", "assistant_content": "goodbye"}, "h2")
        self.queue.enqueue("k3", "claude", "s-other", None, 0, {"user_content": "x", "assistant_content": "y"}, "h3")

        result = worker.flush_session("codex", "s-flush")
        self.assertEqual(result["flushed"], 2)
        self.assertEqual(result["session_id"], "s-flush")

        # 验证 codex/s-flush 已全部 done，claude/s-other 仍是 pending
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT session_id, status FROM capture_events WHERE source_agent = 'codex'")
            rows = cursor.fetchall()
            for sess_id, status in rows:
                self.assertEqual(status, "done")

            cursor.execute("SELECT status FROM capture_events WHERE source_agent = 'claude'")
            self.assertEqual(cursor.fetchone()[0], "pending")

    def test_backoff_state_loaded_on_start(self):
        """Worker 启动时加载持久化的退避状态"""
        self.queue.set_backoff_state("codex", 5, datetime.now().isoformat())
        worker = self._make_worker()
        worker.start()
        self.assertTrue(worker._should_backoff("codex"))
        worker.stop()

    def test_backoff_state_cleared_on_success(self):
        """Worker 成功后清除退避状态"""
        worker = self._make_worker()
        self.queue.set_backoff_state("codex", 3, datetime.now().isoformat())
        worker.start()

        # 直接调用 _record_success 验证数据库清除
        worker._record_success("codex")
        worker.stop()

        state = self.queue.get_backoff_state("codex")
        self.assertEqual(state["error_count"], 0)


class TestCaptureServiceSyncLogLookup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = CaptureQueue(db_path=str(self.db_path))
        _FAKE_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_sync_log_records_memos_uids(self):
        """sync_log 能反查 source/session/turn → memos_uids"""
        mock_client = Mock()
        mock_client._sanitize = lambda x: x
        mock_client.save_long_content.return_value = [
            Mock(uid="uid-1"), Mock(uid="uid-2")
        ]

        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            from core.sync_framework.sync_engine import SyncEngine
            from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

            engine = SyncEngine(client=mock_client, db_path=str(self.sync_db_path))

        class FakeSource(AgentSource):
            @property
            def name(self): return "codex"
            @property
            def model_tag(self): return "codex"
            def discover_sessions(self): return []
            def parse_turns(self, path): return []

        source = FakeSource()
        session = SessionInfo(session_id="sess-lookup", source_path=Path("/tmp/fake"))
        long_content = "x" * 9000
        turn = Turn(
            turn_number=5,
            user_content=long_content,
            assistant_content="hi there",
        )
        result = engine.sync_single_turn(source, session, turn, incremental=False)

        self.assertEqual(result.action, "new")
        self.assertEqual(len(result.memos_uids), 2)

        # 验证 sync_log 能反查
        with sqlite3.connect(str(self.sync_db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT memos_uids FROM sync_log WHERE agent_name = ? AND session_id = ? AND turn_number = ?",
                ("codex", "sess-lookup", 5),
            )
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            uids = json.loads(row[0])
            self.assertEqual(uids, ["uid-1", "uid-2"])


class TestUnifiedContentHash(unittest.TestCase):
    """统一 content_hash：CaptureService 和 SyncEngine 必须计算相同值"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-1")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_capture_service_and_sync_engine_same_hash(self):
        """CaptureService 入队的 content_hash 与 SyncEngine 写入 sync_log 的一致"""
        from core.sync_framework.sync_engine import SyncEngine, compute_content_hash

        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            engine = SyncEngine(client=self.mock_client, db_path=str(self.sync_db_path))

        from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

        class FakeSource(AgentSource):
            @property
            def name(self): return "codex"
            @property
            def model_tag(self): return "codex"
            def discover_sessions(self): return []
            def parse_turns(self, path): return []

        source = FakeSource()
        session = SessionInfo(session_id="sess-hash", source_path=Path("/tmp/fake"))
        turn = Turn(turn_number=0, user_content="hello", assistant_content="hi there")

        # SyncEngine 计算的 hash
        result = engine.sync_single_turn(source, session, turn, incremental=False)
        engine_hash = result.content_hash

        # compute_content_hash 直接计算的 hash
        direct_hash = compute_content_hash(
            user_content="hello",
            assistant_content="hi there",
            turn_number=0,
            model_tag="codex",
        )

        self.assertEqual(engine_hash, direct_hash)
        self.assertIsNotNone(direct_hash)
        self.assertEqual(len(direct_hash), 16)


class TestPerSourceQueueLimit(unittest.TestCase):
    """per-source 队列上限"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_per_source_backpressure(self):
        """单个 source 超过 per_source_max_queue_depth 时返回 backpressure"""
        fake_config = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_config._values["capture.per_source_max_queue_depth"] = 2
        fake_config._values["capture.max_queue_depth"] = 10000
        with patch("core.sync_framework.capture_queue.get_config", return_value=fake_config):
            q = CaptureQueue(db_path=str(self.db_path))

            q.enqueue("k1", "codex", "s1", None, 0, {}, "h1")
            q.enqueue("k2", "codex", "s1", None, 1, {}, "h2")
            # codex 已满
            status = q.enqueue("k3", "codex", "s1", None, 2, {}, "h3")
            self.assertEqual(status, "backpressure")

            # 但其他 source 仍可以入队
            status2 = q.enqueue("k4", "claude", "s1", None, 0, {}, "h4")
            self.assertEqual(status2, "queued")


class TestCaptureSessionStatusPriority(unittest.TestCase):
    """capture_session 状态优先级：backpressure > queued > error > duplicate"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def test_backpressure_takes_priority_over_duplicate(self):
        """全部 backpressure 时应返回 backpressure，不是 duplicate"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["capture.max_queue_depth"] = 2
        queue = CaptureQueue(db_path=str(self.db_path))

        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg):
            service = CaptureService(queue=queue, start_worker=False)
            # 先占满队列（不经过 capture_turn 避免 dedupe_key 冲突）
            queue.enqueue("k0", "codex", "s0", None, 0, {}, "h0")
            queue.enqueue("k1", "codex", "s0", None, 1, {}, "h1")

            result = service.capture_session(
                source_agent="codex",
                session_id="sess-bp",
                turns=[
                    {"turn_number": 2, "user_content": "a", "assistant_content": "b"},
                    {"turn_number": 3, "user_content": "c", "assistant_content": "d"},
                ],
            )
            self.assertEqual(result["status"], "backpressure")
            self.assertGreaterEqual(result["backpressure_count"], 1)


class TestAsyncEndSession(unittest.TestCase):
    """end_session 改为异步：只写标记，不阻塞等待 Memos 写入"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        # 停止 worker，释放数据库连接
        if CaptureService._instance and CaptureService._instance.worker_pool:
            try:
                CaptureService._instance.worker_pool.stop()
            except Exception:
                pass
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def test_end_session_returns_fast(self):
        """end_session 应在 < 200ms 内返回"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        queue = CaptureQueue(db_path=str(self.db_path))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=queue)

        # 先入队一些事件
        queue.enqueue("k1", "codex", "s-end", None, 0, {"user_content": "hi"}, "h1")

        start = time.time()
        result = service.end_session("codex", "s-end")
        elapsed_ms = (time.time() - start) * 1000

        self.assertLess(elapsed_ms, 200, f"end_session 阻塞: {elapsed_ms:.1f}ms")
        self.assertEqual(result["status"], "ok")

    def test_end_session_creates_marker(self):
        """end_session 会写入 session_end_events 标记"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        queue = CaptureQueue(db_path=str(self.db_path))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=queue)

        service.end_session("codex", "s-marker")
        markers = queue.get_session_end_markers()
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["source_agent"], "codex")
        self.assertEqual(markers[0]["session_id"], "s-marker")


class TestSessionEndMarkerPriority(unittest.TestCase):
    """Worker 优先处理带 session_end 标记的 session"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = CaptureQueue(db_path=str(self.db_path))
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-1")
        self._worker = None

    def tearDown(self):
        if self._worker:
            try:
                self._worker.stop()
            except Exception:
                pass
        self.tmpdir.cleanup()

    def test_worker_prioritizes_session_end(self):
        """有 end_session 标记的 session 会被优先 dequeue"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            from core.sync_framework.sync_engine import SyncEngine
            engine = SyncEngine(client=self.mock_client, db_path=str(self.sync_db_path))
            worker = CaptureWorkerPool(queue=self.queue, sync_engine=engine)

        # 入队两个 session
        self.queue.enqueue("k1", "codex", "s-normal", None, 0, {"user_content": "normal"}, "h1")
        self.queue.enqueue("k2", "codex", "s-end", None, 0, {"user_content": "end"}, "h2")

        # 给 s-end 打标记
        self.queue.mark_session_end("codex", "s-end")

        # 手动处理一个批次
        worker._process_one_batch()
        self._worker = worker

        # s-end 应该被优先处理（状态变为 done）
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT session_id, status FROM capture_events WHERE source_agent = 'codex'"
            )
            rows = {r[0]: r[1] for r in cursor.fetchall()}

        # s-end 应该已经处理完
        self.assertEqual(rows.get("s-end"), "done")


class TestPerSourceConcurrency(unittest.TestCase):
    """per_source_concurrency 通过 Semaphore 真正落地"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = CaptureQueue(db_path=str(self.db_path))
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-1")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_source_semaphore_limits_concurrency(self):
        """同一 source 的并发被限制在 per_source_concurrency"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["capture.per_source_concurrency"] = 1
        with patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            from core.sync_framework.sync_engine import SyncEngine
            engine = SyncEngine(client=self.mock_client, db_path=str(self.sync_db_path))
            worker = CaptureWorkerPool(queue=self.queue, sync_engine=engine)

        # 创建信号量
        sem = worker._get_source_semaphore("codex")
        self.assertEqual(sem._value, 1)

        # 获取后应该变 0
        self.assertTrue(sem.acquire(blocking=False))
        self.assertEqual(sem._value, 0)
        self.assertFalse(sem.acquire(blocking=False))
        sem.release()


class TestProducerOnlyMode(unittest.TestCase):
    """MCP producer模式：CaptureService(start_worker=False) 不启动 Worker"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        if CaptureService._instance and CaptureService._instance.worker_pool:
            try:
                CaptureService._instance.worker_pool.stop()
            except Exception:
                pass
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def test_start_worker_false_does_not_start_workers(self):
        """start_worker=False 时 worker_pool 不启动"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(start_worker=False)
        self.assertFalse(service.worker_pool._running)

    def test_start_worker_true_starts_workers(self):
        """start_worker=True 时 worker_pool 启动"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(start_worker=True)
        self.assertTrue(service.worker_pool._running)
        service.worker_pool.stop()

    def test_singleton_lazy_start_worker(self):
        """先 producer 后 consumer，singleton 能补启动 worker"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            prod = CaptureService(start_worker=False)
            self.assertFalse(prod.worker_pool._running)

            # 模拟 daemon 以 consumer 模式获取同一 singleton
            cons = CaptureService(start_worker=True)
            self.assertTrue(cons.worker_pool._running)
            cons.worker_pool.stop()


class TestDequeueFair(unittest.TestCase):
    """dequeue_fair 实现 round-robin 公平调度"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.queue = CaptureQueue(db_path=str(self.db_path))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_fair_dequeue_distributes_across_sources(self):
        """高流量来源不会独占 batch"""
        # aider 入队 8 条
        for i in range(8):
            self.queue.enqueue(f"a{i}", "aider", "s1", None, i, {}, "h")
        # codex 入队 2 条
        for i in range(2):
            self.queue.enqueue(f"c{i}", "codex", "s2", None, i, {}, "h")

        events = self.queue.dequeue_fair(limit=6)

        # 应该两个来源都有事件
        sources = [e["source_agent"] for e in events]
        self.assertIn("aider", sources)
        self.assertIn("codex", sources)

        # 公平性核心：两个来源都有事件，不会只有一个来源独占
        aider_count = sum(1 for s in sources if s == "aider")
        codex_count = sum(1 for s in sources if s == "codex")
        self.assertGreater(aider_count, 0)
        self.assertGreater(codex_count, 0)
        # aider 不能独占全部 6 条
        self.assertLess(aider_count, 6)
        self.assertEqual(len(events), 6)

    def test_fair_dequeue_falls_back_to_global_order(self):
        """round-robin 取完后用全局顺序补充"""
        # 只有一个来源
        for i in range(5):
            self.queue.enqueue(f"k{i}", "codex", "s1", None, i, {}, "h")

        events = self.queue.dequeue_fair(limit=3)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["source_agent"] == "codex" for e in events))


class TestEndToEndMCPToSyncLog(unittest.TestCase):
    """端到端：MCP capture_turn → Queue → Worker → SyncEngine → sync_log"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        if CaptureService._instance and CaptureService._instance.worker_pool:
            try:
                CaptureService._instance.worker_pool.stop()
            except Exception:
                pass
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def test_full_pipeline_writes_sync_log(self):
        """完整链路：MCP 上报 → Worker 消费 → sync_log 有记录"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        mock_client = Mock()
        mock_client._sanitize = lambda x: x
        mock_client.save.return_value = Mock(uid="uid-e2e-1")

        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            from core.sync_framework.sync_engine import SyncEngine

            # 1. MCP producer 上报（不启动 worker）
            producer = CaptureService(start_worker=False)
            result = producer.capture_turn(
                source_agent="codex",
                session_id="sess-e2e",
                turn_number=0,
                user_content="hello e2e",
                assistant_content="hi there",
            )
            self.assertEqual(result["status"], "queued")

            # 2. 手动启动 worker 消费（模拟 daemon consumer）
            engine = SyncEngine(client=mock_client, db_path=str(self.sync_db_path))
            worker = CaptureWorkerPool(queue=producer.queue, sync_engine=engine)
            worker.start()

            # 等待 worker 处理（给足时间，避免 flaky）
            for _ in range(30):
                time.sleep(0.2)
                # 检查事件是否已处理完
                if producer.queue.get_pending_count() == 0:
                    break
            worker.stop()

            # 3. 验证 sync_log 有记录
            with sqlite3.connect(str(self.sync_db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT content_hash, status FROM sync_log WHERE agent_name = ? AND session_id = ? AND turn_number = ?",
                    ("codex", "sess-e2e", 0),
                )
                row = cursor.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[1], "new")

            # 4. 再次上报同一 turn，应返回 duplicate（capture_events + sync_log 双重去重）
            result2 = producer.capture_turn(
                source_agent="codex",
                session_id="sess-e2e",
                turn_number=0,
                user_content="hello e2e",
                assistant_content="hi there",
            )
            self.assertEqual(result2["status"], "duplicate")
            self.assertTrue(result2["duplicate"])


if __name__ == "__main__":
    unittest.main()
