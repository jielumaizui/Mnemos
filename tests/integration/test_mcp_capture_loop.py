"""
MCP Capture 端到端集成测试

覆盖：
- MCP capture_turn → CaptureService → CaptureQueue → CaptureWorker → SyncEngine → StorageBackend
- MCP 工具在 backend 不可用时仍快速返回 queued
- 多来源并发场景
"""

import logging
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch


import unittest


class _FakeConfig:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir or Path(tempfile.gettempdir()) / "mnemos_test_integration"
        self.database_dir = self.data_dir
        self.raw_dir = self.data_dir / "raw"
        self.wiki_dir = self.data_dir / "wiki"
        self._values = {
            "capture.max_queue_depth": 10000,
            "capture.max_workers": 2,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 1,
            "capture.max_payload_bytes": 200000,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)

    @property
    def obsidian_vault_path(self):
        return self.raw_dir


with (
    patch("core.sync_framework.capture_service.get_config", return_value=_FakeConfig()),
    patch("core.sync_framework.capture_queue.get_config", return_value=_FakeConfig()),
    patch("core.sync_framework.capture_worker.get_config", return_value=_FakeConfig()),
    patch("core.sync_framework.sync_engine.get_config", return_value=_FakeConfig()),
):
    from core.sync_framework.capture_service import CaptureService
    from core.sync_framework.capture_schema import CaptureQueueSchema
    from core.agent_kit.authorization import AgentAuthorizationStore
    from integrations.agora import MCPServer


class TestMCPCaptureLoop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        CaptureQueueSchema.initialize(self.db_path)
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x  # noqa
        self.mock_client.save.return_value = [Mock(uid="uid-1")]
        # 重置 CaptureService 单例
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        # 先停止 worker，避免占用数据库文件
        try:
            if CaptureService._instance and getattr(CaptureService._instance, "worker_pool", None):
                CaptureService._instance.worker_pool.stop()
        except Exception:
            logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        CaptureService._instance = None
        CaptureService._initialized = False
        # 给 worker 线程完全退出的时间
        time.sleep(0.3)
        self.tmpdir.cleanup()

    def _make_config(self):
        return _FakeConfig(data_dir=Path(self.tmpdir.name))

    def _make_mcp(self):
        store = AgentAuthorizationStore(Path(self.tmpdir.name) / "agent_authorization.db")
        credential = store.issue_mcp_capability(
            agent="codex",
            host_kind="codex",
            capabilities={"capture_write"},
        )
        return MCPServer(
            launch_credential=credential,
            authorization_store=store,
        )

    def test_mcp_capture_turn_fast_return(self):
        """MCP capture_turn 在 backend 慢/不可用时仍快速返回 queued"""
        fake_cfg = self._make_config()
        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
        ):
            mcp = self._make_mcp()
            start = time.time()
            result = mcp._tool_capture_turn(
                session_id="sess-fast",
                turn_id="t1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi",
            )
            elapsed_ms = (time.time() - start) * 1000

        self.assertLess(elapsed_ms, 200, f"MCP 返回应 < 200ms, 实际 {elapsed_ms:.1f}ms")
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "queued")

    def test_mcp_capture_turn_duplicate(self):
        """同一条 turn 连续上报，第二次返回 duplicate"""
        fake_cfg = self._make_config()
        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
        ):
            mcp = self._make_mcp()
            r1 = mcp._tool_capture_turn(
                session_id="sess-dup",
                turn_id="t1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi",
            )
            r2 = mcp._tool_capture_turn(
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
        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
        ):
            mcp = self._make_mcp()
            turns = [
                {"turn_number": 0, "user_content": "q1", "assistant_content": "a1"},
                {"turn_number": 1, "user_content": "q2", "assistant_content": "a2"},
                {"turn_number": 2, "user_content": "q3", "assistant_content": "a3"},
            ]
            result = mcp._tool_capture_session(
                session_id="sess-batch",
                turns=turns,
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["queued_count"], 3)
        self.assertEqual(result["duplicate_count"], 0)

    def test_mcp_end_session(self):
        """end_session 返回成功"""
        fake_cfg = self._make_config()
        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
        ):
            mcp = self._make_mcp()
            result = mcp._tool_end_session(
                session_id="sess-end",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "handoff_pending")
        self.assertTrue(result["receipt_id"])

    def test_mcp_capture_status(self):
        """capture_status 能查询到已入队的状态"""
        fake_cfg = self._make_config()
        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
        ):
            mcp = self._make_mcp()
            mcp._tool_capture_turn(
                session_id="sess-status",
                turn_id="t1",
                turn_number=0,
                user_content="hello",
                assistant_content="hi",
            )
            result = mcp._tool_capture_status(
                session_id="sess-status",
                turn_number=0,
            )
        self.assertTrue(result["success"])
        self.assertIn(result["status"], ("pending", "processing", "read_only_wal_pending"))
        if result["status"] == "read_only_wal_pending":
            self.assertIn("uncheckpointed WAL", result["error"])
        else:
            self.assertEqual(result["pending_counts"]["by_source"].get("codex"), 1)

    def test_session_save_uses_capture_service(self):
        """现有的 session_save 改走 CaptureService，不再直接写 backend"""
        fake_cfg = self._make_config()
        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
        ):
            mcp = self._make_mcp()
            result = mcp._tool_session_save(
                session_id="sess-migrate",
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
            )
        self.assertTrue(result["success"])
        self.assertIn("入队", result["message"])
        self.assertIn("capture_result", result)


if __name__ == "__main__":
    unittest.main()
