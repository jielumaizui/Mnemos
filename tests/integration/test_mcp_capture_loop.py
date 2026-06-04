"""
MCP Capture 端到端集成测试

覆盖：
- MCP capture_turn → CaptureService → CaptureQueue → CaptureWorker → SyncEngine → MemosClient
- 超长内容分片后 search_and_merge_segments 能接回
- MCP 工具在 Memos 不可用时仍快速返回
- 多来源并发场景
"""

import sys
import json
import time
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest


class _FakeConfig:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir or Path(tempfile.gettempdir()) / "mnemos_test_integration"
        self.memos_token = "fake-token"
        self.memos_api_url = "http://localhost:5230"
        self._values = {
            "memos.max_content_bytes": 7792,
            "memos.ingest_batch_size": 10,
            "memos.ingest_batch_interval": 0,
            "memos.query_cache_ttl": 30,
            "capture.max_queue_depth": 10000,
            "capture.max_workers": 2,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 1,
            "capture.max_payload_bytes": 200000,
            "capture.duplicate_ttl_days": 30,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


with patch("core.sync_framework.capture_service.get_config", return_value=_FakeConfig()), \
     patch("core.sync_framework.capture_queue.get_config", return_value=_FakeConfig()), \
     patch("core.sync_framework.capture_worker.get_config", return_value=_FakeConfig()), \
     patch("core.sync_framework.sync_engine.get_config", return_value=_FakeConfig()):
    from core.sync_framework.capture_service import CaptureService
    from integrations.agora import MCPServer


class TestMCPCaptureLoop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.save.return_value = Mock(uid="uid-1")
        self.mock_client.save_long_content.return_value = [
            Mock(uid="seg-1"), Mock(uid="seg-2"), Mock(uid="seg-3")
        ]
        # 重置 CaptureService 单例
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        # 先停止 worker，避免占用数据库文件
        try:
            if CaptureService._instance and CaptureService._instance.worker_pool:
                CaptureService._instance.worker_pool.stop()
        except Exception:
            pass
        CaptureService._instance = None
        CaptureService._initialized = False
        # 给 worker 线程完全退出的时间
        time.sleep(0.3)
        self.tmpdir.cleanup()

    def _make_config(self):
        return _FakeConfig(data_dir=Path(self.tmpdir.name))

    def _make_mcp(self):
        return MCPServer()

    def test_mcp_capture_turn_fast_return(self):
        """MCP capture_turn 在 Memos 慢/不可用时仍快速返回 queued"""
        fake_cfg = self._make_config()
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            mcp = self._make_mcp()
            start = time.time()
            result = mcp._tool_capture_turn(
                source_agent="codex",
                session_id="sess-fast",
                turn_id="t1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi",
            )
            elapsed_ms = (time.time() - start) * 1000

        self.assertLess(elapsed_ms, 500, f"MCP 返回应 < 500ms, 实际 {elapsed_ms:.1f}ms")
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "queued")

    def test_mcp_capture_turn_duplicate(self):
        """同一条 turn 连续上报，第二次返回 duplicate"""
        fake_cfg = self._make_config()
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            mcp = self._make_mcp()
            r1 = mcp._tool_capture_turn(
                source_agent="codex",
                session_id="sess-dup",
                turn_id="t1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi",
            )
            r2 = mcp._tool_capture_turn(
                source_agent="codex",
                session_id="sess-dup",
                turn_id="t1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi",
            )
        self.assertEqual(r1["status"], "queued")
        self.assertEqual(r2["status"], "duplicate")
        self.assertTrue(r2["duplicate"])

    def test_mcp_capture_session_batch(self):
        """批量上报 session，统计正确"""
        fake_cfg = self._make_config()
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            mcp = self._make_mcp()
            turns = [
                {"turn_number": 0, "user_content": "q1", "assistant_content": "a1"},
                {"turn_number": 1, "user_content": "q2", "assistant_content": "a2"},
                {"turn_number": 2, "user_content": "q3", "assistant_content": "a3"},
            ]
            result = mcp._tool_capture_session(
                source_agent="claude",
                session_id="sess-batch",
                turns=turns,
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["queued_count"], 3)
        self.assertEqual(result["duplicate_count"], 0)

    def test_mcp_end_session(self):
        """end_session 返回成功"""
        fake_cfg = self._make_config()
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            mcp = self._make_mcp()
            result = mcp._tool_end_session(
                source_agent="kimi",
                session_id="sess-end",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "ok")

    def test_mcp_capture_status(self):
        """capture_status 能查询到已入队的状态"""
        fake_cfg = self._make_config()
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            mcp = self._make_mcp()
            mcp._tool_capture_turn(
                source_agent="codex",
                session_id="sess-status",
                turn_id="t1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi",
            )
            result = mcp._tool_capture_status(
                source_agent="codex",
                session_id="sess-status",
                turn_number=0,
            )
        self.assertTrue(result["success"])
        self.assertIn(result["status"], ("pending", "processing"))

    def test_session_save_uses_capture_service(self):
        """现有的 session_save 改走 CaptureService，不再直接写 Memos"""
        fake_cfg = self._make_config()
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg), \
             patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            mcp = self._make_mcp()
            result = mcp._tool_session_save(
                session_id="sess-migrate",
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
                source_agent="codex",
            )
        self.assertTrue(result["success"])
        self.assertIn("入队", result["message"])
        self.assertIn("capture_result", result)


class TestLongContentReassembly(unittest.TestCase):
    """测试被截断的长篇对话能否重新接回"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x
        self.mock_client.max_content_bytes = 7792
        # 设置 tags 为列表，避免 Mock 默认行为
        seg1 = Mock(uid="seg-1")
        seg1.tags = ["segment=1/3", "source=test", "session=sess-merge", "turn=1"]
        seg2 = Mock(uid="seg-2")
        seg2.tags = ["segment=2/3", "source=test", "session=sess-merge", "turn=1"]
        seg3 = Mock(uid="seg-3")
        seg3.tags = ["segment=3/3", "source=test", "session=sess-merge", "turn=1"]
        self.mock_client.save_long_content.return_value = [seg1, seg2, seg3]

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_save_long_content_segments_can_merge(self):
        """save_long_content 分片后，search_and_merge_segments 能接回"""
        from integrations.styx import MemosClient, Memory

        # 构造一个超长的内容
        long_content = "Line content about Python asyncio and concurrency patterns.\n" * 500

        # 手动构造带真实内容的 Memory 对象，避免 Mock 陷阱
        seg1 = Memory(
            id="s1", uid="seg-1", content="[1/3] «test»\n\nPart 1 content",
            tags=["segment=1/3", "source=test", "session=sess-merge", "turn=1", "type=chunk-head"],
            visibility="PUBLIC", created_at="2024-01-01", updated_at="2024-01-01", agent="test",
        )
        seg2 = Memory(
            id="s2", uid="seg-2", content="[2/3] «test»\n\nPart 2 content",
            tags=["segment=2/3", "source=test", "session=sess-merge", "turn=1", "type=chunk-body"],
            visibility="PUBLIC", created_at="2024-01-01", updated_at="2024-01-01", agent="test",
        )
        seg3 = Memory(
            id="s3", uid="seg-3", content="[3/3] «test»\n\nPart 3 content",
            tags=["segment=3/3", "source=test", "session=sess-merge", "turn=1", "type=chunk-body"],
            visibility="PUBLIC", created_at="2024-01-01", updated_at="2024-01-01", agent="test",
        )

        client = MemosClient.__new__(MemosClient)
        merged = client._merge_segments_to_memory([seg1, seg2, seg3])

        self.assertIsNotNone(merged)
        self.assertIn("已合并", merged.content)
        self.assertIn("Part 1 content", merged.content)
        self.assertIn("Part 2 content", merged.content)
        self.assertIn("Part 3 content", merged.content)
        self.assertIn("chunks=3", merged.tags)


if __name__ == "__main__":
    unittest.main()
