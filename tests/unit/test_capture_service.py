"""
CaptureService / CaptureQueue / CaptureWorkerPool 单元测试

覆盖项：
- capture_turn 去重（同 payload 10 次只入队一次）
- capture_turn 内容变化后允许重新入队
- capture_turn 队列积压时仍快速返回
- Worker 对大内容通过 StorageBackend.save 直接保存
- Worker 隔离来源失败（Codex 失败不影响 Claude）
- Worker 保持 session turn 顺序
- daemon 重启后 pending 队列恢复
- sync_log 反查 source/session/turn → backend_uids
"""

import hashlib
import json
import logging
import os
import time
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


import unittest

import pytest

from core.ops.durable_io import DurableIOError


class _FakeConfig:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir or Path(tempfile.gettempdir()) / "mnemos_test_capture"
        self.database_dir = self.data_dir
        self.obsidian_vault_path = self.data_dir / "vault"
        self._values = {
            "capture.max_queue_depth": 10000,
            "capture.per_source_max_queue_depth": 1000,
            "capture.max_workers": 2,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 1,
            "capture.max_payload_bytes": 200000,
            "raw_event_store.enabled": True,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


_FAKE_CONFIG = _FakeConfig()

with (
    patch("core.sync_framework.capture_service.get_config", return_value=_FAKE_CONFIG),
    patch("core.sync_framework.capture_queue.get_config", return_value=_FAKE_CONFIG),
    patch("core.sync_framework.capture_worker.get_config", return_value=_FAKE_CONFIG),
    patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG),
):
    from core.sync_framework.capture_service import CaptureService
    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_worker import CaptureWorkerPool
    from core.sync_framework.capture_schema import CaptureQueueSchema
    from core.sync_framework.capture_maintenance import CaptureRetentionMaintenance


def _open_queue(db_path: Path) -> CaptureQueue:
    CaptureQueueSchema.initialize(db_path)
    return CaptureQueue(db_path=str(db_path))


def _enqueue_raw(queue: CaptureQueue, *args, **kwargs) -> str:
    """Translate legacy fixture labels into explicit test Raw revision IDs."""
    if kwargs:
        raw_revision_id = kwargs.pop("dedupe_key")
        return queue.enqueue(raw_revision_id=raw_revision_id, **kwargs)
    (
        raw_revision_id,
        source_agent,
        session_id,
        turn_id,
        turn_number,
        payload,
        content_hash,
    ) = args
    return queue.enqueue(
        source_agent=source_agent,
        session_id=session_id,
        turn_id=turn_id,
        turn_number=turn_number,
        payload=payload,
        content_hash=content_hash,
        raw_revision_id=raw_revision_id,
    )


class TestCaptureQueue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        CaptureQueueSchema.initialize(self.db_path)
        self.queue = _open_queue(self.db_path)

    def tearDown(self):
        self.queue.close()
        self.tmpdir.cleanup()

    def test_enqueue_and_dequeue(self):
        """入队后出队能取到"""
        status = _enqueue_raw(self.queue,
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
        _enqueue_raw(self.queue,
                     dedupe_key="key-dup",
                     source_agent="codex",
                     session_id="sess-1",
                     turn_id="t1",
                     turn_number=0,
                     payload={},
                     content_hash="abc",
                     )
        status = _enqueue_raw(self.queue,
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
            q2 = _open_queue(self.db_path)
            _enqueue_raw(q2, "k1", "codex", "s1", None, 0, {}, "h1")
            _enqueue_raw(q2, "k2", "codex", "s1", None, 1, {}, "h2")
            status = _enqueue_raw(q2, "k3", "codex", "s1", None, 2, {}, "h3")
            self.assertEqual(status, "backpressure")

    def test_pending_count(self):
        """pending 统计正确"""
        _enqueue_raw(self.queue, "k1", "codex", "s1", None, 0, {}, "h1")
        _enqueue_raw(self.queue, "k2", "claude", "s2", None, 0, {}, "h2")
        self.assertEqual(self.queue.get_pending_count(), 2)
        self.assertEqual(self.queue.get_pending_count("codex"), 1)
        self.assertEqual(
            self.queue.get_pending_counts_by_source(),
            {"claude": 1, "codex": 1},
        )

    def test_daemon_restart_recovery(self):
        """daemon 重启后 pending 队列可恢复"""
        _enqueue_raw(self.queue, "k1", "codex", "s1", None, 0, {}, "h1")
        _enqueue_raw(self.queue, "k2", "codex", "s1", None, 1, {}, "h2")

        # 模拟重启：创建新的 CaptureQueue 实例，指向同一个 db
        q2 = _open_queue(self.db_path)
        self.assertEqual(q2.get_pending_count(), 2)
        events = q2.dequeue(limit=10)
        self.assertEqual(len(events), 2)

    def test_reset_processing_to_pending(self):
        """崩溃恢复：processing 状态回退到 pending"""
        _enqueue_raw(self.queue, "k1", "codex", "s1", None, 0, {}, "h1")
        # 模拟出队后崩溃（状态变成 processing）
        self.queue.dequeue(limit=10)
        self.assertEqual(self.queue.get_pending_count(), 0)

        # 模拟重启后恢复
        reset_count = self.queue.reset_processing_to_pending()
        self.assertEqual(reset_count, 1)
        self.assertEqual(self.queue.get_pending_count(), 1)

    def test_dequeue_by_session(self):
        """按 session 过滤出队"""
        _enqueue_raw(self.queue, "k1", "codex", "s1", None, 0, {}, "h1")
        _enqueue_raw(self.queue, "k2", "codex", "s2", None, 0, {}, "h2")
        _enqueue_raw(self.queue, "k3", "claude", "s1", None, 0, {}, "h3")

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
        _enqueue_raw(self.queue, "k1", "codex", "s1", None, 0, {}, "h1")
        # 标记为 done
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE capture_events SET status = 'done', created_at = '2000-01-01' "
                "WHERE raw_revision_id = 'k1'"
            )
            conn.commit()
        maintenance = CaptureRetentionMaintenance(
            config=_FakeConfig(data_dir=Path(self.tmpdir.name))
        )
        plan = maintenance.plan(
            payload_retention_days=1,
            artifact_retention_days=30,
            artifact_max_total_bytes=10 * 1024 * 1024,
        )
        result = maintenance.apply(plan)
        self.assertEqual(result["deleted_payloads"], 1)
        self.assertEqual(self.queue.get_pending_count(), 0)


class TestCaptureServiceDedup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = _open_queue(self.db_path)
        # 确保 data_dir 存在
        _FAKE_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
        # 重置单例
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        # 停止可能启动的 worker，防止线程泄漏
        if CaptureService._instance and CaptureService._instance.worker_pool:
            try:
                CaptureService._instance.worker_pool.close()
            except Exception:
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        CaptureService._instance = None
        CaptureService._initialized = False
        self.queue.close()
        self.tmpdir.cleanup()

    def _make_service(self, start_worker: bool = True):
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=start_worker)
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

    def test_injected_service_config_reaches_worker_and_sync_engine(self):
        service = self._make_service(start_worker=False)

        self.assertEqual(service.config.database_dir, Path(self.tmpdir.name))
        self.assertEqual(service.worker_pool.config.database_dir, Path(self.tmpdir.name))
        self.assertEqual(service.sync_engine.config.database_dir, Path(self.tmpdir.name))

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

    def test_get_pending_counts_returns_total_and_by_source(self):
        """CaptureService pending 统计是状态/MCP 可观测入口。"""
        service = self._make_service(start_worker=False)
        service.capture_turn(
            source_agent="codex",
            session_id="sess-counts",
            turn_number=0,
            user_content="hello",
            assistant_content="hi",
        )
        service.capture_turn(
            source_agent="claude",
            session_id="sess-counts",
            turn_number=1,
            user_content="question",
            assistant_content="answer",
        )

        counts = service.get_pending_counts()

        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["by_source"], {"claude": 1, "codex": 1})

    def test_oversized_truncated_messages_use_full_hash_for_dedup(self):
        """P114: 超大消息被截断后，应使用完整内容哈希去重，避免前缀相同误判重复。"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["capture.max_payload_bytes"] = 200
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        prefix = "x" * 250
        r1 = service.capture_turn(
            source_agent="codex",
            session_id="sess-114",
            turn_number=0,
            user_content=prefix,
            assistant_content="answer about topic A" * 10,
        )
        self.assertEqual(r1["status"], "queued")
        # 从队列中确认已被截断
        events = self.queue.dequeue(limit=1)
        self.assertTrue(events[0]["payload"]["completeness"].get("truncated"))

        r2 = service.capture_turn(
            source_agent="codex",
            session_id="sess-114",
            turn_number=1,
            user_content=prefix,
            assistant_content="answer about topic B" * 10,
        )
        # 截断后前缀相同，但完整内容不同，不应被误判为重复
        self.assertEqual(r2["status"], "queued")

    def test_capture_turn_preserves_extended_payload(self):
        """结构化对话字段入队后不丢失"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        attachment = Path(self.tmpdir.name) / "report.txt"
        attachment.write_text("2 passed", encoding="utf-8")
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)
        r = service.capture_turn(
            source_agent="claude",
            session_id="sess-extended",
            turn_number=0,
            user_content="run tests",
            assistant_content="done",
            tool_calls=[{"name": "pytest", "input": {"path": "tests"}}],
            tool_results=[{"stdout": "2 passed", "stderr": "", "tool_use_id": "tool-1"}],
            reasoning="checked failing path",
            attachments=[{"path": str(attachment)}],
            raw_event_refs=[{"type": "tool_result"}],
            source_files=["/tmp/session.jsonl"],
            completeness={
                "visible_text": "full",
                "tool_results": "full",
                "reasoning": "full",
                "truncated": False,
            },
        )
        self.assertEqual(r["status"], "queued")

        events = self.queue.dequeue(limit=1)
        payload = events[0]["payload"]
        self.assertEqual(payload["tool_results"][0]["stdout"], "2 passed")
        self.assertEqual(payload["reasoning"], "")
        self.assertIn("reasoning_artifact_path", payload["metadata"])
        self.assertIn("reasoning_sha256", payload["metadata"])
        self.assertEqual(payload["tool_calls"][0]["name"], "pytest")
        for key in (
            "tool_calls",
            "tool_results",
            "reasoning",
            "attachments",
            "raw_event_refs",
            "source_files",
            "completeness",
        ):
            self.assertNotIn(key, payload["metadata"])
        self.assertEqual(payload["completeness"]["tool_results"], "full")
        self.assertEqual(payload["completeness"]["reasoning"], "artifact")
        artifact_refs = payload["metadata"]["artifact_refs"]
        self.assertGreaterEqual(len(artifact_refs), 2)
        self.assertTrue(
            any(ref["artifact_type"] == "tool_result" for ref in artifact_refs),
            artifact_refs,
        )
        self.assertTrue(
            any(ref["artifact_type"] == "reasoning" for ref in artifact_refs),
            artifact_refs,
        )
        self.assertTrue(
            all(ref["uri"].startswith("mnemos-artifact://claude/") for ref in artifact_refs),
            artifact_refs,
        )
        self.assertTrue(
            all(len(ref.get("sha256", "")) == 64 for ref in artifact_refs),
            artifact_refs,
        )
        self.assertEqual(len(payload["metadata"]["reasoning_sha256"]), 64)
        self.assertEqual(payload["completeness"]["artifact_refs_count"], len(artifact_refs))

    def test_oversized_payload_keeps_full_artifact_and_completeness(self):
        """超长内容必须完整落 artifact，并在 payload 中标记完整性。"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["capture.max_payload_bytes"] = 2000
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        full_user = "用户原文-" + ("甲" * 5000)
        full_assistant = "助手原文-" + ("乙" * 5000)
        result = service.capture_turn(
            source_agent="kimi",
            session_id="sess-oversized",
            turn_number=0,
            user_content=full_user,
            assistant_content=full_assistant,
        )

        self.assertEqual(result["status"], "queued")
        payload = self.queue.dequeue(limit=1)[0]["payload"]
        artifact_path = Path(payload["metadata"]["artifact_path"])
        self.assertTrue(artifact_path.exists())
        artifact_text = artifact_path.read_text(encoding="utf-8")
        self.assertIn(full_user, artifact_text)
        self.assertIn(full_assistant, artifact_text)
        self.assertTrue(payload["completeness"]["truncated"])
        self.assertEqual(payload["completeness"]["artifact_path"], str(artifact_path))
        self.assertIn(payload["completeness"]["visible_text"], {"artifact", "artifact_summary"})
        artifact_refs = payload["metadata"]["artifact_refs"]
        self.assertTrue(any(ref["artifact_type"] == "capture_artifact" for ref in artifact_refs))

    def test_managed_artifacts_are_source_scoped_immutable_and_path_safe(self):
        data_dir = Path(self.tmpdir.name) / "managed-artifacts"
        fake_cfg = _FakeConfig(data_dir=data_dir)
        fake_cfg._values["capture.max_payload_bytes"] = 2000
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        first_user = "first-" + ("甲" * 3000)
        second_user = "second-" + ("乙" * 3000)
        calls = (
            ("claude", "../../escape-target", first_user),
            ("kimi", "../../escape-target", first_user),
            ("claude", "../../escape-target", second_user),
        )
        for source_agent, session_id, user_content in calls:
            result = service.capture_turn(
                source_agent=source_agent,
                session_id=session_id,
                turn_number=0,
                user_content=user_content,
                assistant_content="answer",
            )
            self.assertEqual(result["status"], "queued")

        events = self.queue.dequeue(limit=10)
        paths = [Path(event["payload"]["metadata"]["artifact_path"]) for event in events]
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(set(paths)), 3)
        root = (data_dir / "capture_artifacts").absolute()
        self.assertTrue(all(path.absolute().is_relative_to(root) for path in paths))
        self.assertFalse((Path(self.tmpdir.name) / "escape-target").exists())
        artifact_texts = [path.read_text(encoding="utf-8") for path in paths]
        self.assertEqual(
            sum(first_user in artifact_text for artifact_text in artifact_texts),
            2,
        )
        self.assertEqual(
            sum(second_user in artifact_text for artifact_text in artifact_texts),
            1,
        )
        self.assertEqual(
            sum(
                '- source_agent: "claude"' in artifact_text
                for artifact_text in artifact_texts
            ),
            2,
        )
        self.assertEqual(
            sum(
                '- source_agent: "kimi"' in artifact_text
                for artifact_text in artifact_texts
            ),
            1,
        )
        claude_paths = [
            path
            for path, artifact_text in zip(paths, artifact_texts)
            if '- source_agent: "claude"' in artifact_text
        ]
        kimi_path = next(
            path
            for path, artifact_text in zip(paths, artifact_texts)
            if '- source_agent: "kimi"' in artifact_text
        )
        self.assertEqual(len(claude_paths), 2)
        self.assertEqual(claude_paths[0].parent, claude_paths[1].parent)
        self.assertNotEqual(claude_paths[0].parent, kimi_path.parent)

    def test_capture_strips_caller_owned_artifact_metadata(self):
        data_dir = Path(self.tmpdir.name) / "metadata-boundary"
        fake_cfg = _FakeConfig(data_dir=data_dir)
        sentinel = Path(self.tmpdir.name) / "foreign-secret.txt"
        sentinel.write_text("do-not-read-or-rewrite", encoding="utf-8")
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        result = service.capture_turn(
            source_agent="codex",
            session_id="metadata-injection",
            turn_number=0,
            user_content="user",
            assistant_content="assistant",
            reasoning="system-owned reasoning",
            metadata={
                "artifact_path": str(sentinel),
                "reasoning_artifact_path": str(sentinel),
                "reasoning_sha256": "f" * 64,
                "artifact_refs": [{"path": str(sentinel)}],
                "capture_artifact_sha256": "d" * 64,
                "capture_mode": "forged",
                "cognitive_capture_event_id": "forged-capture-event",
                "cognitive_queue_event_id": "forged-queue-event",
                "full_content_hash": "e" * 64,
                "logical_event_id": "forged-logical-event",
                "raw_event_id": "forged-raw-event",
                "raw_event_status": "forged",
            },
            completeness={
                "artifact_path": str(sentinel),
                "artifact_refs_count": 999,
            },
        )

        self.assertEqual(result["status"], "queued")
        payload = self.queue.dequeue(limit=1)[0]["payload"]
        reasoning_path = Path(payload["metadata"]["reasoning_artifact_path"])
        self.assertNotIn("artifact_path", payload["metadata"])
        self.assertNotEqual(reasoning_path, sentinel)
        self.assertTrue(reasoning_path.is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-read-or-rewrite")
        self.assertNotEqual(
            payload["metadata"]["raw_event_id"],
            "forged-raw-event",
        )
        raw_turn = service.raw_store.get_turn(payload["metadata"]["raw_event_id"])
        self.assertIsNotNone(raw_turn)
        self.assertNotEqual(
            raw_turn["metadata"].get("raw_event_id"),
            "forged-raw-event",
        )
        self.assertEqual(payload["metadata"]["capture_artifact_sha256"], "")
        self.assertNotEqual(payload["metadata"]["capture_mode"], "forged")
        self.assertNotEqual(payload["metadata"]["full_content_hash"], "e" * 64)
        self.assertEqual(
            payload["metadata"]["reasoning_sha256"],
            hashlib.sha256(b"system-owned reasoning").hexdigest(),
        )
        self.assertNotEqual(
            payload["metadata"]["cognitive_capture_event_id"],
            "forged-capture-event",
        )
        self.assertNotEqual(
            payload["metadata"]["cognitive_queue_event_id"],
            "forged-queue-event",
        )
        self.assertNotEqual(
            payload["metadata"]["logical_event_id"],
            "forged-logical-event",
        )
        self.assertNotEqual(payload["metadata"]["raw_event_id"], "forged-raw-event")
        self.assertEqual(payload["metadata"]["raw_event_status"], "recorded")
        raw_turn = service.raw_store.get_turn(result["raw_event_id"])
        self.assertIsNotNone(raw_turn)
        raw_metadata = raw_turn["metadata"]
        self.assertEqual(raw_metadata["capture_mode"], "canonical_raw")
        self.assertNotEqual(raw_metadata["full_content_hash"], "e" * 64)
        for caller_owned_key in (
            "artifact_path",
            "cognitive_capture_event_id",
            "cognitive_queue_event_id",
            "logical_event_id",
            "raw_event_id",
            "raw_event_status",
        ):
            self.assertNotIn(caller_owned_key, raw_metadata)
        self.assertEqual(payload["completeness"]["artifact_refs_count"], 1)
        self.assertNotIn("artifact_path", payload["completeness"])
        self.assertNotEqual(
            payload["metadata"]["artifact_refs"][0].get("path"),
            str(sentinel),
        )

    def test_invalid_turn_identity_has_no_artifact_raw_or_queue_effect(self):
        data_dir = Path(self.tmpdir.name) / "invalid-turn"
        fake_cfg = _FakeConfig(data_dir=data_dir)
        fake_cfg._values["capture.max_payload_bytes"] = 10
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        for invalid in (-1, True, "../../turn"):
            result = service.capture_turn(
                source_agent="codex",
                session_id="invalid-turn",
                turn_number=invalid,
                user_content="oversized",
                assistant_content="payload",
                reasoning="reasoning",
            )
            self.assertEqual(result["status"], "error")
        self.assertEqual(self.queue.get_pending_count(), 0)
        self.assertFalse((data_dir / "capture_artifacts").exists())

    def test_raw_event_store_keeps_full_payload_before_queue_truncation(self):
        """canonical raw store 保存截断前全文；队列 payload 可继续截断。"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["capture.max_payload_bytes"] = 2000
        fake_cfg._values["raw_event_store.enabled"] = True
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        full_user = "用户原文-" + ("甲" * 5000)
        full_assistant = "助手原文-" + ("乙" * 5000)
        result = service.capture_turn(
            source_agent="kimi",
            session_id="sess-raw-store",
            turn_number=0,
            user_content=full_user,
            assistant_content=full_assistant,
            completeness={"visible_text": "host_provided"},
        )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["raw_event_status"], "recorded")
        self.assertTrue(result["raw_event_id"])
        self.assertEqual(result["source_event_id"], result["raw_event_id"])
        self.assertEqual(result["provenance_id"], result["raw_event_id"])
        payload = self.queue.dequeue(limit=1)[0]["payload"]
        self.assertTrue(payload["completeness"]["truncated"])

        row = service.raw_store._pool.get_conn().execute(  # noqa: SLF001
            "SELECT event_id FROM raw_turns WHERE source_agent=? AND session_id=? AND turn_number=?",
            ("kimi", "sess-raw-store", 0),
        ).fetchone()
        self.assertIsNotNone(row)
        raw_turn = service.raw_store.get_turn(row[0])
        self.assertEqual(raw_turn["user_content"], full_user)
        self.assertEqual(raw_turn["assistant_content"], full_assistant)
        self.assertEqual(raw_turn["completeness_status"], "derived")

    def test_capture_turn_blocks_queue_when_raw_store_write_fails(self):
        """raw store 启用但写失败时，不得继续生成正式 capture event。"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["raw_event_store.enabled"] = True
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        class FailingRawStore:
            def upsert_turn(self, **kwargs):
                raise sqlite3.OperationalError("raw db locked")

        service.raw_store = FailingRawStore()
        result = service.capture_turn(
            source_agent="kimi",
            session_id="sess-raw-fail",
            turn_number=1,
            user_content="hello",
            assistant_content="hi",
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["raw_event_status"], "failed")
        self.assertEqual(self.queue.get_pending_count(), 0)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT source_agent, session_id, turn_number, error
                FROM capture_raw_failures
                WHERE session_id = ?
                """,
                ("sess-raw-fail",),
            ).fetchone()
        self.assertEqual(row[0], "kimi")
        self.assertEqual(row[1], "sess-raw-fail")
        self.assertEqual(row[2], 1)
        self.assertIn("raw db locked", row[3])

    def test_extended_capture_hash_matches_sync_engine(self):
        """带结构化字段的 CaptureService hash 与 SyncEngine 最终 hash 一致"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=self.queue, start_worker=False)

        result = service.capture_turn(
            source_agent="claude",
            session_id="sess-hash-extended",
            turn_number=0,
            user_content="run tests",
            assistant_content="done",
            tool_results=[{"stdout": "2 passed", "tool_use_id": "tool-1"}],
            reasoning="artifact this reasoning",
        )
        self.assertEqual(result["status"], "queued")
        event = self.queue.dequeue(limit=1)[0]
        payload = event["payload"]

        from core.sync_framework.sync_engine import SyncEngine
        from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

        class FakeSource(AgentSource):
            @property
            def name(self):
                return "claude"

            @property
            def model_tag(self):
                return "claude"

            def discover_sessions(self):
                return []

            def parse_turns(self, path):
                return []

        mock_client = Mock()
        mock_client._sanitize = lambda x: x  # noqa
        mock_client.list_by_tags.return_value = []
        mock_client.save.return_value = [Mock(uid="uid-1")]
        with patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            engine = SyncEngine(backend=mock_client, db_path=str(self.sync_db_path))

        turn = Turn(
            turn_number=event["turn_number"],
            user_content=payload["user_content"],
            assistant_content=payload["assistant_content"],
            timestamp=payload["timestamp"],
            metadata=payload["metadata"],
            tool_calls=payload["tool_calls"],
            tool_results=payload["tool_results"],
            reasoning=payload["reasoning"],
            attachments=payload["attachments"],
            raw_event_refs=payload["raw_event_refs"],
            source_files=payload["source_files"],
            completeness=payload["completeness"],
        )
        sync_result = engine.sync_single_turn(
            FakeSource(),
            SessionInfo(session_id="sess-hash-extended", source_path=Path("/tmp/session.jsonl")),
            turn,
            incremental=False,
        )
        self.assertEqual(event["content_hash"], sync_result.content_hash)

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
            source_agent="codex",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            user_content="hello",
            assistant_content="hi",
        )
        r2 = service.capture_turn(
            source_agent="claude",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            user_content="hello",
            assistant_content="hi",
        )
        self.assertEqual(r1["status"], "queued")
        self.assertEqual(r2["status"], "queued")

        # 相同内容，不同 session
        r3 = service.capture_turn(
            source_agent="codex",
            session_id="s2",
            turn_id="t1",
            turn_number=0,
            user_content="hello",
            assistant_content="hi",
        )
        self.assertEqual(r3["status"], "queued")


class TestCaptureWorker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = _open_queue(self.db_path)
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x  # noqa
        self.mock_client.list_by_tags.return_value = []
        self.mock_client.save.return_value = [Mock(uid="uid-1")]
        _FAKE_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.queue.close()
        self.tmpdir.cleanup()

    def _make_engine(self):
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["raw_projection.enabled"] = False
        with patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            from core.sync_framework.sync_engine import SyncEngine

            return SyncEngine(backend=self.mock_client, db_path=str(self.sync_db_path))

    def _make_worker(self):
        engine = self._make_engine()
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg):
            pool = CaptureWorkerPool(queue=self.queue, sync_engine=engine)
            return pool

    def test_worker_saves_large_payload(self):
        """大内容通过 StorageBackend.save 直接保存（Obsidian 无长度限制）"""
        worker = self._make_worker()
        long_text = "x" * 15000
        _enqueue_raw(self.queue,
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

        worker._process_one_batch()
        self.mock_client.save.assert_called_once()

    def test_worker_isolates_source_failures(self):
        """Codex 失败不影响 Claude"""
        worker = self._make_worker()

        # 让 codex 的 save 抛异常
        def side_effect(content, tags, title, **kwargs):
            if "agent=codex" in str(tags):
                raise RuntimeError("codex boom")
            return [Mock(uid="uid-ok")]

        self.mock_client.save.side_effect = side_effect

        _enqueue_raw(self.queue,
                     "k1", "codex", "s1", None, 0, {"user_content": "hi", "assistant_content": "hello"}, "h1"
                     )
        _enqueue_raw(self.queue,
                     "k2",
                     "claude",
                     "s2",
                     None,
                     0,
                     {"user_content": "hi", "assistant_content": "hello"},
                     "h2",
                     )

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
        self.mock_client.save

        def tracking_save(content, tags, title, **kwargs):
            # 从 content 中提取 turn 信息
            processed.append(content)
            return [Mock(uid="uid")]

        self.mock_client.save = tracking_save

        _enqueue_raw(self.queue,
                     "k3", "codex", "s1", None, 2, {"user_content": "msg3", "assistant_content": "ok"}, "h3"
                     )
        _enqueue_raw(self.queue,
                     "k1", "codex", "s1", None, 0, {"user_content": "msg1", "assistant_content": "ok"}, "h1"
                     )
        _enqueue_raw(self.queue,
                     "k2", "codex", "s1", None, 1, {"user_content": "msg2", "assistant_content": "ok"}, "h2"
                     )

        worker._process_one_batch()

        # 应该按 turn_number 0, 1, 2 顺序处理
        self.assertIn("msg1", processed[0])
        self.assertIn("msg2", processed[1])
        self.assertIn("msg3", processed[2])

    def test_flush_session_immediate(self):
        """end_session 触发 flush_session 立即处理指定 session"""
        worker = self._make_worker()
        _enqueue_raw(self.queue,
                     "k1",
                     "codex",
                     "s-flush",
                     None,
                     0,
                     {"user_content": "hi", "assistant_content": "hello"},
                     "h1",
                     )
        _enqueue_raw(self.queue,
                     "k2",
                     "codex",
                     "s-flush",
                     None,
                     1,
                     {"user_content": "bye", "assistant_content": "goodbye"},
                     "h2",
                     )
        _enqueue_raw(self.queue,
                     "k3",
                     "claude",
                     "s-other",
                     None,
                     0,
                     {"user_content": "x", "assistant_content": "y"},
                     "h3",
                     )

        result = worker.flush_session("codex", "s-flush")
        self.assertEqual(result["flushed"], 2)
        self.assertEqual(result["session_id"], "s-flush")

        # 验证 codex/s-flush 已全部 done，claude/s-other 仍是 pending
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT session_id, status FROM capture_events WHERE source_agent = 'codex'"
            )
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
        self.queue = _open_queue(self.db_path)
        _FAKE_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.queue.close()
        self.tmpdir.cleanup()

    def test_sync_log_records_backend_uids(self):
        """sync_log 能反查 source/session/turn → backend_uids"""
        mock_client = Mock()
        mock_client._sanitize = lambda x: x  # noqa
        mock_client.list_by_tags.return_value = []
        mock_client.save.return_value = [Mock(uid="uid-1"), Mock(uid="uid-2")]

        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["raw_projection.enabled"] = False
        with patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            from core.sync_framework.sync_engine import SyncEngine
            from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

            engine = SyncEngine(backend=mock_client, db_path=str(self.sync_db_path))

        class FakeSource(AgentSource):
            @property
            def name(self):
                return "codex"

            @property
            def model_tag(self):
                return "codex"

            def discover_sessions(self):
                return []

            def parse_turns(self, path):
                return []

        source = FakeSource()
        session = SessionInfo(session_id="sess-lookup", source_path=Path("/tmp/fake"))
        turn = Turn(
            turn_number=5,
            user_content="hello world",
            assistant_content="hi there",
        )
        result = engine.sync_single_turn(source, session, turn, incremental=False)

        self.assertEqual(result.action, "new")
        self.assertEqual(len(result.backend_uids), 2)

        # 验证 sync_log 能反查
        with sqlite3.connect(str(self.sync_db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT backend_uids FROM sync_log WHERE agent_name = ? AND session_id = ? AND turn_number = ?",  # noqa: E501
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
        self.mock_client._sanitize = lambda x: x  # noqa
        self.mock_client.list_by_tags.return_value = []
        self.mock_client.save.return_value = [Mock(uid="uid-1")]

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_capture_service_and_sync_engine_same_hash(self):
        """CaptureService 入队的 content_hash 与 SyncEngine 写入 sync_log 的一致"""
        from core.sync_framework.sync_engine import SyncEngine, compute_content_hash

        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg):
            engine = SyncEngine(backend=self.mock_client, db_path=str(self.sync_db_path))

        from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

        class FakeSource(AgentSource):
            @property
            def name(self):
                return "codex"

            @property
            def model_tag(self):
                return "codex"

            def discover_sessions(self):
                return []

            def parse_turns(self, path):
                return []

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
            q = _open_queue(self.db_path)

            _enqueue_raw(q, "k1", "codex", "s1", None, 0, {}, "h1")
            _enqueue_raw(q, "k2", "codex", "s1", None, 1, {}, "h2")
            # codex 已满
            status = _enqueue_raw(q, "k3", "codex", "s1", None, 2, {}, "h3")
            self.assertEqual(status, "backpressure")

            # 但其他 source 仍可以入队
            status2 = _enqueue_raw(q, "k4", "claude", "s1", None, 0, {}, "h4")
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
        queue = _open_queue(self.db_path)

        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
        ):
            service = CaptureService(queue=queue, start_worker=False)
            # 先占满队列（不经过 capture_turn 避免 dedupe_key 冲突）
            _enqueue_raw(queue, "k0", "codex", "s0", None, 0, {}, "h0")
            _enqueue_raw(queue, "k1", "codex", "s0", None, 1, {}, "h1")

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
    """end_session 改为异步：只写标记，不阻塞等待 backend 写入"""

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
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def test_end_session_returns_fast(self):
        """end_session 应在 < 200ms 内返回"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        queue = _open_queue(self.db_path)
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(queue=queue)

        # 先入队一些事件
        _enqueue_raw(queue, "k1", "codex", "s-end", None, 0, {"user_content": "hi"}, "h1")

        start = time.time()
        result = service.end_session("codex", "s-end")
        elapsed_ms = (time.time() - start) * 1000

        self.assertLess(elapsed_ms, 200, f"end_session 阻塞: {elapsed_ms:.1f}ms")
        self.assertEqual(result["status"], "handoff_pending")
        self.assertTrue(result["receipt_id"])

    def test_end_session_creates_marker(self):
        """end_session 会写入 session_end_events 标记"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        queue = _open_queue(self.db_path)
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
        self.queue = _open_queue(self.db_path)
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x  # noqa
        self.mock_client.save.return_value = [Mock(uid="uid-1")]
        self._worker = None

    def tearDown(self):
        if self._worker:
            try:
                self._worker.close()
            except Exception:
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        self.queue.close()
        self.tmpdir.cleanup()

    def test_worker_prioritizes_session_end(self):
        """有 end_session 标记的 session 会被优先 dequeue"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with (
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
            patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg),
        ):
            from core.sync_framework.sync_engine import SyncEngine

            engine = SyncEngine(backend=self.mock_client, db_path=str(self.sync_db_path))
            worker = CaptureWorkerPool(queue=self.queue, sync_engine=engine)

        # 入队两个 session
        _enqueue_raw(self.queue, "k1", "codex", "s-normal", None, 0, {"user_content": "normal"}, "h1")
        _enqueue_raw(self.queue, "k2", "codex", "s-end", None, 0, {"user_content": "end"}, "h2")

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

    def test_worker_propagates_session_end_marker_storage_failure(self):
        """读取 marker 失败不能伪装成当前没有待处理 session。"""

        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        worker = CaptureWorkerPool(
            queue=self.queue,
            sync_engine=Mock(),
            config=fake_cfg,
        )
        self._worker = worker

        with (
            patch.object(
                self.queue,
                "get_session_end_markers",
                side_effect=RuntimeError("capture ledger unavailable"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "capture ledger unavailable",
            ),
        ):
            worker._dequeue_session_end_markers()


class TestPerSourceConcurrency(unittest.TestCase):
    """per_source_concurrency 通过 Semaphore 真正落地"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        self.queue = _open_queue(self.db_path)
        self.mock_client = Mock()
        self.mock_client._sanitize = lambda x: x  # noqa
        self.mock_client.save.return_value = [Mock(uid="uid-1")]

    def tearDown(self):
        self.queue.close()
        self.tmpdir.cleanup()

    def test_source_semaphore_limits_concurrency(self):
        """同一 source 的并发被限制在 per_source_concurrency"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        fake_cfg._values["capture.per_source_concurrency"] = 1
        with (
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
            patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg),
        ):
            from core.sync_framework.sync_engine import SyncEngine

            engine = SyncEngine(backend=self.mock_client, db_path=str(self.sync_db_path))
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
        CaptureQueueSchema.initialize(Path(self.tmpdir.name) / "capture_queue.db")
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        if CaptureService._instance and getattr(CaptureService._instance, "worker_pool", None):
            try:
                CaptureService._instance.worker_pool.stop()
            except Exception:
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def test_start_worker_false_does_not_start_workers(self):
        """start_worker=False 时 worker_pool 不启动"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(start_worker=False)
        self.assertFalse(service.worker_pool._running)
        self.assertEqual(service.queue.db_path.parent, fake_cfg.database_dir)
        self.assertEqual(service.sync_engine.config.database_dir, fake_cfg.database_dir)

    def test_exposes_sync_engine_for_l1_audit(self):
        """L1 扫描审计需要访问实际 worker 使用的 SyncEngine。"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        with patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg):
            service = CaptureService(start_worker=False)

        self.assertIs(service.sync_engine, service.worker_pool.engine)
        self.assertTrue(hasattr(service.sync_engine, "record_audit"))

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
        self.queue = _open_queue(self.db_path)

    def tearDown(self):
        self.queue.close()
        self.tmpdir.cleanup()

    def test_fair_dequeue_distributes_across_sources(self):
        """高流量来源不会独占 batch"""
        # aider 入队 8 条
        for i in range(8):
            _enqueue_raw(self.queue, f"a{i}", "aider", "s1", None, i, {}, "h")
        # codex 入队 2 条
        for i in range(2):
            _enqueue_raw(self.queue, f"c{i}", "codex", "s2", None, i, {}, "h")

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
            _enqueue_raw(self.queue, f"k{i}", "codex", "s1", None, i, {}, "h")

        events = self.queue.dequeue_fair(limit=3)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["source_agent"] == "codex" for e in events))


class TestEndToEndMCPToSyncLog(unittest.TestCase):
    """端到端：MCP capture_turn → Queue → Worker → SyncEngine → sync_log"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.sync_db_path = Path(self.tmpdir.name) / "sync_log.db"
        CaptureQueueSchema.initialize(self.db_path)
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        if CaptureService._instance and getattr(CaptureService._instance, "worker_pool", None):
            try:
                CaptureService._instance.worker_pool.stop()
            except Exception:
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        CaptureService._instance = None
        CaptureService._initialized = False
        self.tmpdir.cleanup()

    def test_full_pipeline_writes_sync_log(self):
        """完整链路：MCP 上报 → Worker 消费 → sync_log 有记录"""
        fake_cfg = _FakeConfig(data_dir=Path(self.tmpdir.name))
        mock_client = Mock()
        mock_client._sanitize = lambda x: x  # noqa
        mock_client.save.return_value = [Mock(uid="uid-e2e-1")]

        with (
            patch("core.sync_framework.capture_service.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_queue.get_config", return_value=fake_cfg),
            patch("core.sync_framework.capture_worker.get_config", return_value=fake_cfg),
            patch("core.sync_framework.sync_engine.get_config", return_value=fake_cfg),
        ):
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
            engine = SyncEngine(backend=mock_client, db_path=str(self.sync_db_path))
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
                    "SELECT content_hash, status FROM sync_log WHERE agent_name = ? AND session_id = ? AND turn_number = ?",  # noqa: E501
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


class TestCaptureServiceArtifactCleanup(unittest.TestCase):
    """capture_artifacts 目录 TTL/大小上限清理测试"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "capture_queue.db"
        self.queue = _open_queue(self.db_path)
        CaptureService._instance = None
        CaptureService._initialized = False

    def tearDown(self):
        if CaptureService._instance and getattr(CaptureService._instance, "worker_pool", None):
            try:
                CaptureService._instance.worker_pool.close()
            except Exception:
                pass
        CaptureService._instance = None
        CaptureService._initialized = False
        self.queue.close()
        self.tmpdir.cleanup()

    def test_cleanup_removes_artifacts_older_than_ttl(self):
        """超过 TTL 的 artifact session 目录应被删除"""
        data_dir = Path(self.tmpdir.name) / "artifacts_old"
        data_dir.mkdir(parents=True)
        config = _FakeConfig(data_dir=data_dir)
        CaptureQueueSchema.initialize(config.database_dir / "capture_queue.db")

        artifact_root = data_dir / "capture_artifacts"
        old_session = artifact_root / "old-session"
        old_session.mkdir(parents=True)
        (old_session / "turn_0.md").write_text("old", encoding="utf-8")
        # 修改时间设为 40 天前
        old_mtime = time.time() - 40 * 86400
        os.utime(old_session, (old_mtime, old_mtime))

        new_session = artifact_root / "new-session"
        new_session.mkdir(parents=True)
        (new_session / "turn_0.md").write_text("new", encoding="utf-8")

        maintenance = CaptureRetentionMaintenance(config=config)
        stats = maintenance.apply(
            maintenance.plan(
                payload_retention_days=30,
                artifact_retention_days=30,
                artifact_max_total_bytes=10 * 1024 * 1024 * 1024,
            )
        )

        self.assertEqual(stats["deleted_artifacts"], 1)
        self.assertFalse(old_session.exists())
        self.assertTrue(new_session.exists())

    def test_cleanup_respects_total_size_cap(self):
        """总大小超过上限时，按时间由旧到新删除"""
        data_dir = Path(self.tmpdir.name) / "artifacts_size"
        data_dir.mkdir(parents=True)
        config = _FakeConfig(data_dir=data_dir)
        CaptureQueueSchema.initialize(config.database_dir / "capture_queue.db")

        artifact_root = data_dir / "capture_artifacts"
        for i, name in enumerate(("session-a", "session-b", "session-c")):
            sess_dir = artifact_root / name
            sess_dir.mkdir(parents=True)
            (sess_dir / "turn_0.md").write_text("x" * 1000, encoding="utf-8")
            # session-a 最旧，session-c 最新
            os.utime(sess_dir, (time.time() - (3 - i) * 3600, time.time() - (3 - i) * 3600))

        # 上限设为一个半目录的大小，应删除最旧的 2 个
        maintenance = CaptureRetentionMaintenance(config=config)
        stats = maintenance.apply(
            maintenance.plan(
                payload_retention_days=30,
                artifact_retention_days=30,
                artifact_max_total_bytes=1500,
            )
        )

        self.assertEqual(stats["deleted_artifacts"], 2)
        self.assertFalse((artifact_root / "session-a").exists())
        self.assertFalse((artifact_root / "session-b").exists())
        self.assertTrue((artifact_root / "session-c").exists())

    def test_cleanup_rejects_symlink_candidates_without_touching_target(self):
        data_dir = Path(self.tmpdir.name) / "artifacts_symlink"
        data_dir.mkdir(parents=True)
        config = _FakeConfig(data_dir=data_dir)
        CaptureQueueSchema.initialize(config.database_dir / "capture_queue.db")
        artifact_root = data_dir / "capture_artifacts"
        victim = artifact_root / "victim"
        victim.mkdir(parents=True)
        (victim / "turn.md").write_text("sentinel", encoding="utf-8")
        (artifact_root / "redirect").symlink_to(victim, target_is_directory=True)

        maintenance = CaptureRetentionMaintenance(config=config)
        with pytest.raises(DurableIOError, match="capture_artifact_inventory_unsafe"):
            maintenance.plan(
                payload_retention_days=30,
                artifact_retention_days=0,
                artifact_max_total_bytes=0,
            )
        self.assertEqual(
            (victim / "turn.md").read_text(encoding="utf-8"),
            "sentinel",
        )

    def test_cleanup_skips_candidate_changed_after_plan(self):
        data_dir = Path(self.tmpdir.name) / "artifacts_cas"
        data_dir.mkdir(parents=True)
        config = _FakeConfig(data_dir=data_dir)
        CaptureQueueSchema.initialize(config.database_dir / "capture_queue.db")
        candidate = data_dir / "capture_artifacts" / "session"
        candidate.mkdir(parents=True)
        (candidate / "turn.md").write_text("first", encoding="utf-8")
        old_mtime = time.time() - 40 * 86400
        os.utime(candidate, (old_mtime, old_mtime))

        maintenance = CaptureRetentionMaintenance(config=config)
        plan = maintenance.plan(
            payload_retention_days=30,
            artifact_retention_days=30,
            artifact_max_total_bytes=10 * 1024 * 1024,
        )
        (candidate / "new-generation.md").write_text("late", encoding="utf-8")

        result = maintenance.apply(plan)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["deleted_artifacts"], 0)
        self.assertEqual(result["stale_artifacts"], 1)
        self.assertTrue(candidate.is_dir())
        self.assertEqual(
            (candidate / "new-generation.md").read_text(encoding="utf-8"),
            "late",
        )

    def test_cleanup_receipt_resumes_after_artifact_phase_crash(self):
        data_dir = Path(self.tmpdir.name) / "artifacts_resume"
        data_dir.mkdir(parents=True)
        config = _FakeConfig(data_dir=data_dir)
        db_path = config.database_dir / "capture_queue.db"
        CaptureQueueSchema.initialize(db_path)
        candidate = data_dir / "capture_artifacts" / "session"
        candidate.mkdir(parents=True)
        (candidate / "turn.md").write_text("planned", encoding="utf-8")
        old_mtime = time.time() - 40 * 86400
        os.utime(candidate, (old_mtime, old_mtime))
        maintenance = CaptureRetentionMaintenance(config=config)
        plan = maintenance.plan(
            payload_retention_days=30,
            artifact_retention_days=30,
            artifact_max_total_bytes=10 * 1024 * 1024,
        )

        with patch(
            "core.sync_framework.capture_maintenance.secure_remove_directory_tree",
            side_effect=RuntimeError("artifact phase crashed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "artifact phase crashed"):
                maintenance.apply(plan)

        with sqlite3.connect(str(db_path)) as conn:
            receipt = conn.execute(
                "SELECT status, applied_count FROM capture_maintenance_receipts"
            ).fetchone()
        self.assertEqual(receipt, ("processing", 0))
        self.assertTrue(candidate.is_dir())

        resumed = maintenance.apply(plan)
        self.assertEqual(resumed["status"], "committed")
        self.assertEqual(resumed["deleted_artifacts"], 1)
        self.assertFalse(resumed["replayed"])
        self.assertFalse(candidate.exists())

        replay = maintenance.apply(plan)
        self.assertEqual(replay["status"], "committed")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["already_applied_count"], 1)

    def test_cleanup_rejects_forged_artifact_selection(self):
        data_dir = Path(self.tmpdir.name) / "artifacts_forged_selection"
        data_dir.mkdir(parents=True)
        config = _FakeConfig(data_dir=data_dir)
        CaptureQueueSchema.initialize(config.database_dir / "capture_queue.db")
        candidate = data_dir / "capture_artifacts" / "new-session"
        candidate.mkdir(parents=True)
        (candidate / "turn.md").write_text("new", encoding="utf-8")
        maintenance = CaptureRetentionMaintenance(config=config)
        plan = maintenance.plan(
            payload_retention_days=30,
            artifact_retention_days=30,
            artifact_max_total_bytes=10 * 1024 * 1024,
        )
        self.assertEqual(plan["artifact_candidates"], [])
        plan["artifact_candidates"] = list(plan["artifact_inventory"])
        plan["plan_hash"] = maintenance._plan_hash(plan)  # noqa: SLF001

        with self.assertRaisesRegex(
            ValueError,
            "capture artifact candidate selection is invalid",
        ):
            maintenance.apply(plan)
        self.assertTrue(candidate.is_dir())
