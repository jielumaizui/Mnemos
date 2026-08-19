# -*- coding: utf-8 -*-
"""Unit tests for core/pluggable.py"""

from unittest.mock import MagicMock, patch

import pytest

from core.pluggable import PluggableModule

# ---------------------------------------------------------------------------
# Abstract interface verification
# ---------------------------------------------------------------------------


def test_pluggable_module_is_abc():
    """PluggableModule 应为抽象基类，不能直接实例化。"""
    with pytest.raises(TypeError):
        PluggableModule()


def test_abstract_methods():
    """子类必须实现所有抽象方法。"""

    class PartialImpl(PluggableModule):
        def enable(self):
            pass

    with pytest.raises(TypeError):
        PartialImpl()


class TestConcreteModule:
    """具体子类实现测试"""

    @pytest.fixture
    def module(self):
        """提供完整实现的子类实例。"""

        class TestModule(PluggableModule):
            def __init__(self):
                self._active = False
                self._cfg = {}
                self._events = []

            def enable(self):
                self._active = True

            def disable(self):
                self._active = False

            def configure(self, cfg):
                self._cfg = cfg

            def handle_event(self, event_type, data):
                self._events.append((event_type, data))

        return TestModule()

    def test_enable(self, module):
        """enable 应激活模块。"""
        module.enable()
        assert module._active is True

    def test_disable(self, module):
        """disable 应停用模块。"""
        module.enable()
        module.disable()
        assert module._active is False

    def test_configure(self, module):
        """configure 应接收配置字典。"""
        module.configure({"threshold": 0.8})
        assert module._cfg["threshold"] == 0.8

    def test_handle_event(self, module):
        """handle_event 应接收事件类型和数据。"""
        module.handle_event("page_created", {"page": "test.md"})
        assert len(module._events) == 1
        assert module._events[0] == ("page_created", {"page": "test.md"})


# ---------------------------------------------------------------------------
# _emit_event
# ---------------------------------------------------------------------------


class TestEmitEvent:
    """_emit_event 测试"""

    @pytest.fixture
    def module(self):
        """提供完整实现的子类实例。"""

        class TestModule(PluggableModule):
            def enable(self):
                pass

            def disable(self):
                pass

            def configure(self, cfg):
                pass

            def handle_event(self, event_type, data):
                pass

        return TestModule()

    def test_emit_event_success(self, module):
        """事件总线可用时应发布成功。"""
        mock_bus = MagicMock()
        mock_bus.publish.return_value = "trace-123"
        with patch("core.mnemos_bus.get_event_bus", return_value=mock_bus):
            trace_id = module._emit_event("test_event", {"key": "value"})
        assert trace_id == "trace-123"
        mock_bus.publish.assert_called_once_with("test_event", payload={"key": "value"})

    def test_emit_event_no_bus_returns_none(self, module):
        """事件总线不可用时返回 None。"""
        with patch("core.mnemos_bus.get_event_bus", side_effect=ImportError):
            trace_id = module._emit_event("test_event", {})
        assert trace_id is None

    def test_emit_event_publish_error_returns_none(self, module):
        """publish 失败时返回 None。"""
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = RuntimeError("bus error")
        with patch("core.mnemos_bus.get_event_bus", return_value=mock_bus):
            trace_id = module._emit_event("test_event", {})
        assert trace_id is None
