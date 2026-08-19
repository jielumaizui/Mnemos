"""
Tests for core.sync_framework.capture_worker

Covers: CaptureWorkerPool init/start/stop, flush_session, _should_backoff,
        _process_event, _DynamicAgentSource.
"""

import time
from pathlib import Path
from unittest.mock import Mock, patch


from core.sync_framework.capture_worker import (
    CaptureWorkerPool,
    _DynamicAgentSource,
)


class TestCaptureWorkerPoolInit:
    @patch("core.sync_framework.capture_worker.get_config")
    def test_default_init(self, mock_get_config):
        mock_cfg = Mock()
        mock_cfg.get.side_effect = lambda k, d: {
            "capture.max_workers": 4,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 5,
        }.get(k, d)
        mock_get_config.return_value = mock_cfg

        pool = CaptureWorkerPool(queue=Mock(), sync_engine=Mock())
        assert pool.max_workers == 4
        assert pool._running is False

    def test_init_with_injected_deps(self):
        mock_queue = Mock()
        mock_engine = Mock()
        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=mock_engine)
        assert pool.queue is mock_queue
        assert pool.engine is mock_engine


class TestCaptureWorkerPoolStartStop:
    @patch("core.sync_framework.capture_worker.get_config")
    def test_start_stop(self, mock_get_config):
        mock_cfg = Mock()
        mock_cfg.get.side_effect = lambda k, d: {
            "capture.max_workers": 1,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 5,
        }.get(k, d)
        mock_get_config.return_value = mock_cfg

        mock_queue = Mock()
        mock_queue.reset_processing_to_pending.return_value = 0

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=Mock())
        pool.start()
        assert pool._running is True
        assert len(pool._worker_threads) == 1

        pool.stop()
        assert pool._running is False
        assert len(pool._worker_threads) == 0

    @patch("core.sync_framework.capture_worker.get_config")
    def test_start_idempotent(self, mock_get_config):
        mock_cfg = Mock()
        mock_cfg.get.side_effect = lambda k, d: {
            "capture.max_workers": 1,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 5,
        }.get(k, d)
        mock_get_config.return_value = mock_cfg

        mock_queue = Mock()
        mock_queue.reset_processing_to_pending.return_value = 0

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=Mock())
        pool.start()
        threads_after_first = len(pool._worker_threads)
        pool.start()  # 第二次不应创建新线程
        assert len(pool._worker_threads) == threads_after_first
        pool.stop()

    @patch("core.sync_framework.capture_worker.get_config")
    def test_close(self, mock_get_config):
        mock_cfg = Mock()
        mock_cfg.get.side_effect = lambda k, d: {
            "capture.max_workers": 1,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 5,
        }.get(k, d)
        mock_get_config.return_value = mock_cfg

        mock_queue = Mock()
        mock_queue.reset_processing_to_pending.return_value = 0
        mock_engine = Mock()

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=mock_engine)
        pool.start()
        pool.close()
        assert pool._running is False
        mock_queue.close.assert_called_once()
        mock_engine.close.assert_called_once()


class TestFlushSession:
    def test_flush_no_events(self):
        mock_queue = Mock()
        mock_queue.dequeue_by_session.return_value = []
        pool = CaptureWorkerPool(queue=mock_queue)
        result = pool.flush_session("claude", "sess_1")
        assert result["flushed"] == 0

    @patch("core.sync_framework.registry.SourceRegistry")
    def test_flush_with_events(self, mock_registry):
        mock_queue = Mock()
        mock_queue.dequeue_by_session.return_value = [
            {
                "id": "ev1",
                "source_agent": "claude",
                "session_id": "sess_1",
                "turn_id": "t1",
                "turn_number": 1,
                "payload": {
                    "user_content": "hi",
                    "assistant_content": "hello",
                },
            },
        ]
        mock_engine = Mock()
        mock_result = Mock()
        mock_result.action = "done"
        mock_engine.sync_single_turn.return_value = mock_result

        mock_registry.get.return_value = None

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=mock_engine)
        result = pool.flush_session("claude", "sess_1")
        assert result["flushed"] == 1
        mock_engine.sync_single_turn.assert_called_once()

    def test_flush_with_failed_event(self):
        mock_queue = Mock()
        mock_queue.dequeue_by_session.return_value = [
            {
                "id": "ev1",
                "source_agent": "claude",
                "session_id": "sess_1",
                "turn_id": "t1",
                "turn_number": 1,
                "payload": {"user_content": "hi"},
            },
        ]
        mock_engine = Mock()
        mock_result = Mock()
        mock_result.action = "failed"
        mock_result.error = "test error"
        mock_engine.sync_single_turn.return_value = mock_result

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=mock_engine)
        result = pool.flush_session("claude", "sess_1")
        assert result["failed"] == 1
        mock_queue.update_status.assert_called()

    def test_flush_max_retries_exceeded(self):
        mock_queue = Mock()
        mock_queue.dequeue_by_session.return_value = [
            {
                "id": "ev1",
                "source_agent": "claude",
                "session_id": "sess_1",
                "turn_number": 1,
                "retry_count": 3,
                "payload": {"user_content": "hi"},
            },
        ]
        mock_engine = Mock()
        mock_result = Mock()
        mock_result.action = "failed"
        mock_engine.sync_single_turn.return_value = mock_result

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=mock_engine)
        pool.flush_session("claude", "sess_1")
        # retry_count >= 3 应标记为 failed
        calls = mock_queue.update_status.call_args_list
        assert any(call[0][1] == "failed" for call in calls)

    @patch("core.sync_framework.capture_worker.get_config")
    def test_capture_only_event_creates_intentional_skip_handoff(self, mock_get_config):
        mock_get_config.return_value.get.return_value = True
        queue = Mock()
        queue.create_distillation_handoff.return_value = {"status": "intentional_skip"}
        pool = CaptureWorkerPool(queue=queue, sync_engine=Mock())
        pool._complete_session_end = Mock()
        event = {
            "payload": {"metadata": {"distill_requested": False}},
            "source_agent": "file_ingestor:test",
            "session_id": "doc-session",
        }

        assert pool._try_enqueue_distillation(
            "file_ingestor:test", "doc-session", [event]
        )
        assert queue.create_distillation_handoff.call_args.kwargs["enabled"] is False
        pool._complete_session_end.assert_called_once_with(
            "file_ingestor:test", "doc-session"
        )


class TestShouldBackoff:
    def test_no_errors(self):
        pool = CaptureWorkerPool(queue=Mock(), sync_engine=Mock())
        assert pool._should_backoff("claude") is False

    def test_backoff_active(self):
        pool = CaptureWorkerPool(queue=Mock(), sync_engine=Mock())
        pool._source_errors["claude"] = 1
        pool._source_last_retry["claude"] = time.time()
        assert pool._should_backoff("claude") is True

    def test_backoff_expired(self):
        pool = CaptureWorkerPool(queue=Mock(), sync_engine=Mock())
        pool._source_errors["claude"] = 1
        pool._source_last_retry["claude"] = time.time() - 1000
        assert pool._should_backoff("claude") is False


class TestRecordSuccessError:
    def test_record_error(self):
        mock_queue = Mock()
        pool = CaptureWorkerPool(queue=mock_queue)
        pool._record_error("claude")
        assert pool._source_errors["claude"] == 1
        assert pool._source_last_retry["claude"] > 0
        mock_queue.set_backoff_state.assert_called_once()

    def test_record_success(self):
        mock_queue = Mock()
        pool = CaptureWorkerPool(queue=mock_queue)
        pool._source_errors["claude"] = 3
        pool._source_last_retry["claude"] = time.time()
        pool._record_success("claude")
        assert pool._source_errors["claude"] == 0
        assert pool._source_last_retry["claude"] == 0
        mock_queue.clear_backoff_state.assert_called_once()


class TestProcessBatch:
    def test_process_batch_processes_the_events_it_dequeues(self):
        event = {
            "id": 1,
            "source_agent": "codex",
            "session_id": "sess_1",
            "turn_number": 1,
            "payload": {"user_content": "hi"},
        }
        mock_queue = Mock()
        mock_queue.get_session_end_markers.return_value = []
        mock_queue.list_distillation_handoffs.return_value = []
        mock_queue.dequeue_fair.side_effect = [[event], []]
        mock_queue.get_event_statuses.return_value = {1: "done"}

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=Mock())
        pool._process_session_events = Mock()

        result = pool.process_batch(limit=1)

        assert result == {
            "processed": 1,
            "committed": 1,
            "handoffs": 0,
            "errors": 0,
            "status": "committed",
        }
        pool._process_session_events.assert_called_once_with(
            "codex",
            "sess_1",
            [event],
        )


class TestProcessEvent:
    @patch("core.sync_framework.registry.SourceRegistry")
    def test_process_event_with_registry_source(self, mock_registry):
        mock_source = Mock()
        mock_source.name = "claude"
        mock_source.model_tag = "claude-sonnet"
        mock_registry.get.return_value = mock_source

        mock_queue = Mock()
        mock_engine = Mock()
        mock_result = Mock()
        mock_result.action = "done"
        mock_engine.sync_single_turn.return_value = mock_result

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=mock_engine)
        event = {
            "id": "ev1",
            "source_agent": "claude",
            "session_id": "sess_1",
            "turn_id": "t1",
            "turn_number": 1,
            "payload": {
                "user_content": "hi",
                "assistant_content": "hello",
                "timestamp": "2024-01-01T00:00:00",
                "metadata": {"tool_calls": []},
            },
        }
        pool._process_event(event)
        mock_engine.sync_single_turn.assert_called_once()
        mock_queue.update_status.assert_not_called()

    @patch("core.sync_framework.registry.SourceRegistry")
    def test_process_event_dynamic_source(self, mock_registry):
        mock_registry.get.return_value = None

        mock_queue = Mock()
        mock_engine = Mock()
        mock_result = Mock()
        mock_result.action = "done"
        mock_engine.sync_single_turn.return_value = mock_result

        pool = CaptureWorkerPool(queue=mock_queue, sync_engine=mock_engine)
        event = {
            "id": "ev1",
            "source_agent": "new_agent",
            "session_id": "sess_1",
            "turn_number": 1,
            "payload": {
                "user_content": "hi",
                "model": "test-model",
            },
        }
        pool._process_event(event)
        args = mock_engine.sync_single_turn.call_args[1]
        assert args["source"].name == "new_agent"
        assert args["source"].model_tag == "test-model"


class TestDynamicAgentSource:
    def test_properties(self):
        src = _DynamicAgentSource("my_agent", "gpt-4")
        assert src.name == "my_agent"
        assert src.model_tag == "gpt-4"

    def test_discover_sessions(self):
        src = _DynamicAgentSource("a", "m")
        assert src.discover_sessions() == []

    def test_parse_turns(self):
        src = _DynamicAgentSource("a", "m")
        assert src.parse_turns(Path("/tmp")) == []

    def test_build_dynamic_source_preserves_capture_event_turn_number(self):
        pool = CaptureWorkerPool(queue=Mock(), sync_engine=Mock())
        source = pool._build_dynamic_source(
            "file_ingestor:test",
            "session-1",
            [{"turn_number": 7, "payload": {"user_content": "document"}}],
        )

        turns = source.parse_turns(Path("."))

        assert len(turns) == 1
        assert turns[0].turn_number == 7


class TestSourceSemaphore:
    def test_get_or_create_semaphore(self):
        pool = CaptureWorkerPool(queue=Mock(), sync_engine=Mock())
        sem = pool._get_source_semaphore("claude")
        assert sem is not None
        # 再次获取应返回同一个
        sem2 = pool._get_source_semaphore("claude")
        assert sem is sem2

    def test_per_source_concurrency(self):
        pool = CaptureWorkerPool(queue=Mock(), sync_engine=Mock())
        sem = pool._get_source_semaphore("claude")
        assert sem._value == 1  # 默认 per_source_concurrency = 1
