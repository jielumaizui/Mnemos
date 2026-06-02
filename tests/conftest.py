"""
全局测试配置：自动隔离 EventBus，防止测试写入生产 events.db。

策略：
- autouse fixture 默认 mock publish_event（最外层调用入口）
- test_eventbus_real_loop.py 等需要真实 EventBus 的测试，使用自己的 bus fixture
  直接调用 bus.publish()，不受此 mock 影响
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_eventbus(monkeypatch):
    """所有测试自动隔离：阻止 publish_event 写入 ~/.mnemos/events.db。"""
    monkeypatch.setattr("core.mnemos_bus.publish_event", lambda *a, **k: None)
