"""
全局测试配置：自动隔离 EventBus，防止测试写入生产 events.db。

策略：
- autouse fixture 默认 mock publish_event（最外层调用入口）
- test_eventbus_real_loop.py 等需要真实 EventBus 的测试，使用自己的 bus fixture
  直接调用 bus.publish()，不受此 mock 影响
- Windows 上 SQLite 文件锁问题：monkeypatch tempfile.TemporaryDirectory.cleanup
  使其在遇到 PermissionError 时重试并忽略（测试连接 GC 延迟导致文件锁定）
"""
import gc
import sys
import tempfile
import time

import pytest


# ---- Windows SQLite 文件锁兼容性修复 ----
# 问题：Windows 上 sqlite3 连接在测试 tearDown 时可能仍未被 GC 释放，
#       导致 tempfile.TemporaryDirectory.cleanup() 抛出 PermissionError。
# 修复：在 Windows 上重试 cleanup，给 GC 足够时间释放文件描述符。
_original_cleanup = tempfile.TemporaryDirectory.cleanup


def _patched_cleanup(self):
    if sys.platform != "win32":
        return _original_cleanup(self)
    # Windows：先触发 GC，给 SQLite 连接释放时间
    gc.collect()
    time.sleep(0.05)
    for attempt in range(5):
        try:
            return _original_cleanup(self)
        except (PermissionError, NotADirectoryError):
            # NotADirectoryError: Python 3.10 tempfile 的已知 bug，
            # 当 os.unlink 失败时会错误地对文件路径调用 rmtree
            gc.collect()
            time.sleep(0.2 * (attempt + 1))
    # 最终 fallback：强制删除
    import shutil
    shutil.rmtree(self.name, ignore_errors=True)


tempfile.TemporaryDirectory.cleanup = _patched_cleanup


@pytest.fixture(autouse=True)
def _isolate_eventbus(monkeypatch):
    """所有测试自动隔离：阻止 publish_event 写入 ~/.mnemos/events.db。"""
    monkeypatch.setattr("core.mnemos_bus.publish_event", lambda *a, **k: None)
