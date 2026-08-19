# -*- coding: utf-8 -*-
"""Tests for daemon.runtime.RuntimeContext."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from daemon.runtime import RuntimeContext


class DummyResource:
    def __init__(self):
        self.stopped = False
        self.closed = False
        self.shutdown_called = False

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def shutdown(self):
        self.shutdown_called = True


class TestRuntimeContext:
    def test_register_and_get(self):
        ctx = RuntimeContext()
        resource = object()
        assert ctx.register("r1", resource) is resource
        assert ctx.get("r1") is resource
        assert ctx.get("missing") is None

    def test_resources_closed_in_reverse_registration_order(self):
        ctx = RuntimeContext()
        order = []

        class TrackingResource:
            def __init__(self, name):
                self.name = name

            def close(self):
                order.append(self.name)

        ctx.register("first", TrackingResource("first"))
        ctx.register("second", TrackingResource("second"))
        ctx.register("third", TrackingResource("third"))
        ctx.shutdown()

        assert order == ["third", "second", "first"]

    def test_custom_closer_called(self):
        ctx = RuntimeContext()
        resource = MagicMock()
        closer = MagicMock()
        ctx.register("custom", resource, closer=closer)
        ctx.shutdown()
        closer.assert_called_once_with(resource)

    def test_close_on_shutdown_false_skips_closer(self):
        ctx = RuntimeContext()
        resource = MagicMock()
        closer = MagicMock()
        ctx.register("skip", resource, closer=closer, close_on_shutdown=False)
        ctx.shutdown()
        closer.assert_not_called()

    def test_default_closer_prefers_stop_over_close(self):
        ctx = RuntimeContext()
        resource = MagicMock()
        ctx.register("default", resource)
        ctx.shutdown()
        resource.stop.assert_called_once()
        resource.close.assert_not_called()

    def test_stop_event_set_after_shutdown(self):
        ctx = RuntimeContext()
        assert not ctx.stop_event.is_set()
        ctx.shutdown()
        assert ctx.stop_event.is_set()

    def test_context_manager_calls_shutdown(self):
        ctx = RuntimeContext()
        resource = DummyResource()
        with ctx:
            ctx.register("r", resource)
        assert resource.stopped is True
        assert ctx.stop_event.is_set()

    def test_shutdown_is_idempotent(self):
        ctx = RuntimeContext()
        resource = MagicMock(spec=["close"])
        ctx.register("r", resource)
        ctx.shutdown()
        ctx.shutdown()
        assert resource.close.call_count == 1

    def test_install_signal_handlers_sets_stop_event(self):
        ctx = RuntimeContext()
        with patch("signal.signal") as mock_signal:
            ctx.install_signal_handlers()
            assert mock_signal.call_count == 2
            # 提取 SIGTERM handler 并调用
            calls = {call.args[0]: call.args[1] for call in mock_signal.call_args_list}
            handler = calls.get(15)  # SIGTERM = 15 on Linux/macOS
            if handler is None:
                handler = calls.get(2)  # SIGINT fallback
            # handler sets stop_event asynchronously; we can at least verify no exception
            handler(15, None)
            # handler starts a thread; give it a moment
            ctx.stop_event.wait(timeout=0.5)
        assert ctx.stop_event.is_set()

    def test_reset_singletons_called_on_shutdown(self):
        ctx = RuntimeContext()
        with patch("core.config.reset_config") as mock_reset_config, \
             patch("core.mnemos_bus.reset_event_bus") as mock_reset_bus, \
             patch("core.sync_framework.capture_service.CaptureService.reset_instance") as mock_reset_capture, \
             patch("core.sync_framework.registry.SourceRegistry.reset") as mock_reset_registry:
            ctx.shutdown()
            mock_reset_config.assert_called_once()
            mock_reset_bus.assert_called_once()
            mock_reset_capture.assert_called_once()
            mock_reset_registry.assert_called_once()

    def test_shutdown_logs_errors_but_does_not_raise(self):
        ctx = RuntimeContext()
        failing = MagicMock(spec=["close"])
        failing.close.side_effect = RuntimeError("boom")
        ctx.register("failing", failing)
        # should not raise
        ctx.shutdown()
        failing.close.assert_called_once()

    def test_shutdown_propagates_programming_errors(self):
        ctx = RuntimeContext()
        failing = MagicMock(spec=["close"])
        failing.close.side_effect = AssertionError("closer bug")
        ctx.register("failing", failing)

        with pytest.raises(AssertionError, match="closer bug"):
            ctx.shutdown()

    def test_resource_none_is_skipped(self):
        ctx = RuntimeContext()
        ctx.register("none", None)
        # should not raise
        ctx.shutdown()
