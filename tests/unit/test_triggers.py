# -*- coding: utf-8 -*-
"""
触发器系统 (core/sync_framework/triggers.py) 单元测试

覆盖项：
- BaseTrigger 初始化与配置
- BaseTrigger._backoff_delay() 指数退避计算
- BaseTrigger._execute_callback() 错误隔离与计数恢复
- WatchdogTrigger 初始化默认值与自定义配置
- WatchdogTrigger._on_event() / _fire() 去抖动逻辑
- WatchdogTrigger.stop() 清理待处理定时器
- PollingTrigger._scan() 新文件检测与变更检测
- PollingTrigger._scan() 已删除文件清理
- PollingTrigger 模式过滤 (pattern)
- PollingTrigger._load_state() / _save_state() SQLite 持久化
- TriggerDispatcher.register() 根据策略创建正确触发器类型
- TriggerDispatcher.start_all() / stop_all() 生命周期管理
- 错误隔离：单个回调失败不影响其他回调执行
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.db_utils import SqlitePool
from core.ops.durable_io import DurableIOError

_FAKE_CONFIG = None


def _get_fake_config():
    """延迟初始化，避免在模块导入时创建临时目录"""
    global _FAKE_CONFIG
    if _FAKE_CONFIG is None:
        _FAKE_CONFIG = MagicMock()
        _FAKE_CONFIG.data_dir = Path(tempfile.gettempdir()) / "mnemos_test_triggers"
        _FAKE_CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
        _FAKE_CONFIG.database_dir = _FAKE_CONFIG.data_dir
    return _FAKE_CONFIG


@pytest.fixture(scope="module", autouse=True)
def patch_config():
    """模块级 fixture：为所有测试 mock get_config"""
    fake = _get_fake_config()
    with patch("core.sync_framework.triggers.get_config", return_value=fake):
        yield


# 必须在 patch 之后导入
with patch("core.sync_framework.triggers.get_config", return_value=_get_fake_config()):
    from core.sync_framework.triggers import (
        BaseTrigger,
        WatchdogTrigger,
        PollingTrigger,
        HybridTrigger,
        TriggerDispatcher,
    )


class _ConcreteTrigger(BaseTrigger):
    """用于测试 BaseTrigger 的具体子类"""

    def start(self, watch_path: Path):
        pass

    def stop(self):
        pass


# ---------------------------------------------------------------------------
# BaseTrigger
# ---------------------------------------------------------------------------


class TestBaseTrigger:
    """BaseTrigger 基类行为测试"""

    def test_init_default_values(self):
        """默认初始化：回调、名称、运行状态、错误计数、退避上限"""
        callback = MagicMock()
        trigger = _ConcreteTrigger(callback=callback, source_name="test_src")

        assert trigger._callback is callback
        assert trigger._source_name == "test_src"
        assert trigger._running is False
        assert trigger._error_count == 0
        assert trigger._max_backoff == 300

    def test_backoff_delay_exponential_growth(self):
        """指数退避：5s -> 10s -> 20s -> 40s -> 80s -> 160s -> 300s(上限)"""
        trigger = _ConcreteTrigger(callback=lambda x: None)

        expected = [5, 10, 20, 40, 80, 160, 300, 300]
        for i, exp_delay in enumerate(expected):
            trigger._error_count = i
            actual = trigger._backoff_delay()
            assert actual == exp_delay, f"error_count={i}: expected {exp_delay}, got {actual}"

    def test_backoff_delay_max_cap(self):
        """退避延迟不超过 300 秒上限"""
        trigger = _ConcreteTrigger(callback=lambda x: None)
        trigger._error_count = 100

        assert trigger._backoff_delay() == 300

    def test_execute_callback_success_decrements_error_count(self):
        """回调成功后错误计数减 1（最低到 0）"""
        trigger = _ConcreteTrigger(callback=lambda x: None)
        trigger._error_count = 3

        trigger._execute_callback("/tmp/file.txt")

        assert trigger._error_count == 2

    def test_execute_callback_success_clamps_to_zero(self):
        """回调成功时错误计数不会低于 0"""
        trigger = _ConcreteTrigger(callback=lambda x: None)
        trigger._error_count = 0

        trigger._execute_callback("/tmp/file.txt")

        assert trigger._error_count == 0

    def test_execute_callback_failure_increments_error_count(self):
        """回调失败后错误计数加 1"""

        def boom(_):
            raise RuntimeError("boom")

        trigger = _ConcreteTrigger(callback=boom)
        trigger._execute_callback("/tmp/file.txt")

        assert trigger._error_count == 1

    def test_execute_callback_failure_does_not_raise(self):
        """回调失败被捕获，不会向外抛出异常"""

        def boom(_):
            raise RuntimeError("boom")

        trigger = _ConcreteTrigger(callback=boom)
        # 不应抛出异常
        trigger._execute_callback("/tmp/file.txt")
        trigger._execute_callback("/tmp/file.txt")

        assert trigger._error_count == 2


# ---------------------------------------------------------------------------
# WatchdogTrigger
# ---------------------------------------------------------------------------


class TestWatchdogTrigger:
    """WatchdogTrigger 文件监视触发器测试"""

    def test_init_default_values(self):
        """默认初始化：事件类型为 modified，去抖动 5.0 秒"""
        trigger = WatchdogTrigger(callback=lambda x: None)

        assert trigger._events == ["modified"]
        assert trigger._debounce == 5.0
        assert trigger._pending == {}

    def test_init_custom_values(self):
        """自定义初始化：事件类型和去抖动时间"""
        trigger = WatchdogTrigger(
            callback=lambda x: None,
            source_name="custom",
            events=["created", "modified"],
            debounce=2.5,
        )

        assert trigger._events == ["created", "modified"]
        assert trigger._debounce == 2.5
        assert trigger._source_name == "custom"

    def test_on_event_creates_pending_timer(self):
        """_on_event 为文件创建待处理定时器"""
        trigger = WatchdogTrigger(callback=lambda x: None, source_name="test", debounce=0.1)

        trigger._on_event("/tmp/test.json")

        assert "/tmp/test.json" in trigger._pending
        assert isinstance(trigger._pending["/tmp/test.json"], threading.Timer)
        trigger.stop()

    def test_on_event_same_file_resets_timer(self):
        """同一文件多次事件：取消旧定时器，只保留最新一个"""
        calls = []
        trigger = WatchdogTrigger(
            callback=lambda p: calls.append(p), source_name="test", debounce=0.05
        )

        trigger._on_event("/tmp/file.json")
        trigger._on_event("/tmp/file.json")
        trigger._on_event("/tmp/file.json")

        # 只应有一个待处理定时器
        assert len(trigger._pending) == 1
        assert "/tmp/file.json" in trigger._pending

        # 等待去抖动时间后只触发一次回调
        time.sleep(0.15)
        assert calls.count("/tmp/file.json") == 1
        trigger.stop()

    def test_on_event_different_files_create_separate_timers(self):
        """不同文件的事件各自独立去抖动"""
        trigger = WatchdogTrigger(callback=lambda x: None, source_name="test", debounce=0.1)

        trigger._on_event("/tmp/a.json")
        trigger._on_event("/tmp/b.json")

        assert len(trigger._pending) == 2
        assert "/tmp/a.json" in trigger._pending
        assert "/tmp/b.json" in trigger._pending
        trigger.stop()

    def test_stop_clears_pending_timers(self):
        """stop() 取消所有待处理定时器并清空字典"""
        trigger = WatchdogTrigger(callback=lambda x: None, source_name="test", debounce=10.0)

        trigger._on_event("/tmp/a.json")
        trigger._on_event("/tmp/b.json")
        assert len(trigger._pending) == 2

        trigger.stop()

        assert trigger._pending == {}
        assert trigger._running is False

    def test_fire_removes_from_pending(self):
        """_fire 执行后从 pending 中移除该文件"""
        calls = []
        trigger = WatchdogTrigger(
            callback=lambda p: calls.append(p), source_name="test", debounce=0.01
        )

        trigger._on_event("/tmp/file.json")
        time.sleep(0.05)

        assert "/tmp/file.json" not in trigger._pending
        assert calls == ["/tmp/file.json"]
        trigger.stop()


# ---------------------------------------------------------------------------
# PollingTrigger
# ---------------------------------------------------------------------------


class TestPollingTrigger:
    """PollingTrigger 轮询触发器测试"""

    def test_load_state_late_abort_rolls_back_schema_and_preserves_memory(
        self,
        tmp_path: Path,
    ) -> None:
        class LateStateAbort(BaseException):
            pass

        database = tmp_path / "polling_state.db"
        with sqlite3.connect(database) as preimage:
            preimage.execute(
                "CREATE TABLE preimage_sentinel (value TEXT PRIMARY KEY)"
            )
            preimage.execute(
                "INSERT INTO preimage_sentinel(value) VALUES ('unchanged')"
            )

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters=(), /):  # type: ignore[override]
                result = super().execute(sql, parameters)
                if "SELECT path, mtime FROM polling_state" in str(sql):
                    raise LateStateAbort("sentinel polling state failure")
                return result

        connection = sqlite3.connect(database, factory=FailingConnection)

        class SingleConnectionPool:
            def get_conn(self) -> sqlite3.Connection:
                return connection

            def close(self) -> None:
                connection.close()

        trigger = PollingTrigger(
            callback=lambda _path: None,
            source_name="atomic-load",
        )
        trigger._pool.close()
        trigger._db_path = database
        trigger._pool = SingleConnectionPool()  # type: ignore[assignment]
        trigger._seen = {"memory-preimage": 7.0}

        with pytest.raises(LateStateAbort, match="sentinel polling state failure"):
            trigger._load_state()

        assert trigger._seen == {"memory-preimage": 7.0}
        assert trigger._state_loaded is False
        with sqlite3.connect(database) as observed:
            objects = observed.execute(
                """
                SELECT type, name FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
        assert objects == [("table", "preimage_sentinel")]
        trigger.close()

    def test_load_state_storage_failure_is_typed_and_never_empty(
        self,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "polling_state.db"

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters=(), /):  # type: ignore[override]
                if "SELECT path, mtime FROM polling_state" in str(sql):
                    raise sqlite3.OperationalError("sentinel unavailable")
                return super().execute(sql, parameters)

        connection = sqlite3.connect(database, factory=FailingConnection)

        class SingleConnectionPool:
            def get_conn(self) -> sqlite3.Connection:
                return connection

            def close(self) -> None:
                connection.close()

        trigger = PollingTrigger(
            callback=lambda _path: None,
            source_name="typed-load",
        )
        trigger._pool.close()
        trigger._db_path = database
        trigger._pool = SingleConnectionPool()  # type: ignore[assignment]
        trigger._seen = {"memory-preimage": 7.0}

        with pytest.raises(
            DurableIOError,
            match="polling_state_read_unavailable",
        ):
            trigger._load_state()

        assert trigger._seen == {"memory-preimage": 7.0}
        assert trigger._state_loaded is False
        trigger.close()

    def test_save_state_empty_snapshot_deletes_stale_rows(
        self,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "polling_state.db"
        trigger = PollingTrigger(
            callback=lambda _path: None,
            source_name="empty-save",
        )
        trigger._pool.close()
        trigger._db_path = database
        trigger._pool = SqlitePool(database)
        trigger._load_state()
        trigger._seen = {"stale-path": 1.0}
        trigger._save_state()
        trigger._seen = {}

        trigger._save_state()

        connection = trigger._pool.get_conn()
        rows = connection.execute(
            "SELECT path FROM polling_state WHERE source='empty-save'"
        ).fetchall()
        assert rows == []
        trigger.close()

    def test_save_state_late_abort_restores_previous_source_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        class LateStateAbort(BaseException):
            pass

        database = tmp_path / "polling_state.db"
        with sqlite3.connect(database) as preimage:
            preimage.execute(
                """
                CREATE TABLE polling_state (
                    source TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    PRIMARY KEY (source, path)
                )
                """
            )
            preimage.execute(
                "INSERT INTO polling_state VALUES ('atomic-save', 'old-path', 1.0)"
            )

        class FailingConnection(sqlite3.Connection):
            def executemany(self, sql: str, parameters, /):  # type: ignore[override]
                super().executemany(sql, parameters)
                raise LateStateAbort("sentinel polling state write failure")

        connection = sqlite3.connect(database, factory=FailingConnection)

        class SingleConnectionPool:
            def get_conn(self) -> sqlite3.Connection:
                return connection

            def close(self) -> None:
                connection.close()

        trigger = PollingTrigger(
            callback=lambda _path: None,
            source_name="atomic-save",
        )
        trigger._pool.close()
        trigger._db_path = database
        trigger._pool = SingleConnectionPool()  # type: ignore[assignment]
        trigger._state_loaded = True
        trigger._seen = {"new-path": 2.0}

        with pytest.raises(
            LateStateAbort,
            match="sentinel polling state write failure",
        ):
            trigger._save_state()

        with sqlite3.connect(database) as observed:
            rows = observed.execute(
                "SELECT source, path, mtime FROM polling_state"
            ).fetchall()
        assert rows == [("atomic-save", "old-path", 1.0)]
        trigger.close()

    def test_start_state_failure_never_marks_trigger_running(
        self,
        tmp_path: Path,
    ) -> None:
        trigger = PollingTrigger(
            callback=lambda _path: None,
            source_name="failed-start",
        )
        trigger._load_state = MagicMock(  # type: ignore[method-assign]
            side_effect=DurableIOError("polling_state_read_unavailable")
        )
        trigger.close = MagicMock()  # type: ignore[method-assign]

        with pytest.raises(
            DurableIOError,
            match="polling_state_read_unavailable",
        ):
            trigger.start(tmp_path)

        assert trigger._running is False
        assert trigger._thread is None
        trigger.close.assert_called_once()

    def test_init_default_values(self):
        """默认初始化：轮询间隔 3600 秒，匹配模式 *.txt"""
        trigger = PollingTrigger(callback=lambda x: None)

        assert trigger._interval == 3600
        assert trigger._pattern == "*.txt"
        assert trigger._seen == {}

    def test_init_custom_values(self):
        """自定义初始化：轮询间隔和匹配模式"""
        trigger = PollingTrigger(
            callback=lambda x: None,
            source_name="poll_test",
            interval=60,
            pattern="*.md",
        )

        assert trigger._interval == 60
        assert trigger._pattern == "*.md"
        assert trigger._source_name == "poll_test"

    def test_scan_detects_new_file(self, tmp_path: Path):
        """_scan 检测到新文件时触发回调"""
        calls = []
        trigger = PollingTrigger(
            callback=lambda p: calls.append(p), source_name="test", interval=3600
        )
        trigger._load_state()

        test_file = tmp_path / "new.txt"
        test_file.write_text("hello")

        trigger._scan(tmp_path)

        assert len(calls) == 1
        assert calls[0] == str(test_file)
        trigger.close()

    def test_scan_skips_unchanged_file(self, tmp_path: Path):
        """_scan 对未变更文件不重复触发回调"""
        calls = []
        trigger = PollingTrigger(
            callback=lambda p: calls.append(p), source_name="test", interval=3600
        )
        trigger._load_state()

        test_file = tmp_path / "stable.txt"
        test_file.write_text("hello")

        trigger._scan(tmp_path)
        assert len(calls) == 1

        trigger._scan(tmp_path)
        assert len(calls) == 1  # 不应增加
        trigger.close()

    def test_scan_detects_modified_file(self, tmp_path: Path):
        """_scan 检测到文件修改时再次触发回调"""
        calls = []
        trigger = PollingTrigger(
            callback=lambda p: calls.append(p), source_name="test", interval=3600
        )
        trigger._load_state()

        test_file = tmp_path / "changing.txt"
        test_file.write_text("v1")

        trigger._scan(tmp_path)
        assert len(calls) == 1

        # 修改文件内容（mtime 会变化）
        test_file.write_text("v2")
        trigger._scan(tmp_path)

        assert len(calls) == 2
        trigger.close()

    def test_scan_cleans_deleted_files(self, tmp_path: Path):
        """_scan 清理已删除文件的记录，防止内存无限增长"""
        trigger = PollingTrigger(callback=lambda x: None, source_name="test", interval=3600)
        trigger._load_state()

        test_file = tmp_path / "temp.txt"
        test_file.write_text("hello")

        trigger._scan(tmp_path)
        assert str(test_file) in trigger._seen

        test_file.unlink()
        trigger._scan(tmp_path)

        assert str(test_file) not in trigger._seen
        trigger.close()

    def test_scan_pattern_filtering(self, tmp_path: Path):
        """_scan 只匹配指定模式的文件"""
        calls = []
        trigger = PollingTrigger(
            callback=lambda p: calls.append(p),
            source_name="test",
            interval=3600,
            pattern="*.md",
        )
        trigger._load_state()

        (tmp_path / "a.txt").write_text("txt")
        (tmp_path / "b.md").write_text("md")
        (tmp_path / "c.json").write_text("json")

        trigger._scan(tmp_path)

        assert len(calls) == 1
        assert calls[0].endswith("b.md")
        trigger.close()

    def test_scan_nonexistent_path(self, tmp_path: Path):
        """_scan 对不存在的路径安全返回，不抛出异常"""
        trigger = PollingTrigger(callback=lambda x: None, source_name="test", interval=3600)
        trigger._load_state()

        nonexistent = tmp_path / "does_not_exist"
        # 不应抛出异常
        trigger._scan(nonexistent)
        trigger.close()

    def test_scan_uninspectable_path_fails_instead_of_looking_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from core.ops.durable_io import DurableIOError

        trigger = PollingTrigger(
            callback=lambda x: None,
            source_name="test",
            interval=3600,
        )
        trigger._load_state()
        original_stat = Path.stat

        def denied(path: Path, *args: object, **kwargs: object):
            if path == tmp_path:
                raise PermissionError("sentinel")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)

        with pytest.raises(DurableIOError, match="durable_path_inspection_failed"):
            trigger._scan(tmp_path)
        trigger.close()

    def test_scan_uninspectable_entry_fails_instead_of_skipping_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from core.ops.durable_io import DurableIOError

        target = tmp_path / "entry.txt"
        target.write_text("sentinel", encoding="utf-8")
        trigger = PollingTrigger(
            callback=lambda x: None,
            source_name="test",
            interval=3600,
        )
        trigger._load_state()
        original_stat = Path.stat

        def denied(path: Path, *args: object, **kwargs: object):
            if path == target:
                raise PermissionError("sentinel")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)

        with pytest.raises(
            DurableIOError,
            match="polling_trigger_entry_unavailable",
        ):
            trigger._scan(tmp_path)
        trigger.close()

    def test_state_persistence_roundtrip(self, tmp_path: Path):
        """状态持久化：保存后新触发器能正确加载"""
        trigger1 = PollingTrigger(
            callback=lambda x: None, source_name="persist_test", interval=3600
        )
        trigger1._load_state()

        test_file = tmp_path / "state.txt"
        test_file.write_text("hello")

        trigger1._scan(tmp_path)
        assert str(test_file) in trigger1._seen
        trigger1._save_state()
        trigger1.close()

        # 创建新触发器，加载之前保存的状态
        trigger2 = PollingTrigger(
            callback=lambda x: None, source_name="persist_test", interval=3600
        )
        trigger2._load_state()

        assert str(test_file) in trigger2._seen
        assert trigger2._seen[str(test_file)] == trigger1._seen[str(test_file)]
        trigger2.close()

    def test_state_isolation_between_sources(self, tmp_path: Path):
        """不同 source_name 的状态相互隔离"""
        trigger1 = PollingTrigger(callback=lambda x: None, source_name="src_a", interval=3600)
        trigger1._load_state()

        test_file = tmp_path / "iso.txt"
        test_file.write_text("hello")
        trigger1._scan(tmp_path)
        trigger1._save_state()
        trigger1.close()

        trigger2 = PollingTrigger(callback=lambda x: None, source_name="src_b", interval=3600)
        trigger2._load_state()

        assert str(test_file) not in trigger2._seen
        trigger2.close()

    def test_load_state_creates_table(self, tmp_path: Path):
        """_load_state 自动创建 polling_state 表"""
        trigger = PollingTrigger(callback=lambda x: None, source_name="table_test", interval=3600)
        trigger._load_state()

        conn = trigger._pool.get_conn()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='polling_state'"
        )
        assert cursor.fetchone() is not None
        trigger.close()


# ---------------------------------------------------------------------------
# TriggerDispatcher
# ---------------------------------------------------------------------------


class TestTriggerDispatcher:
    """TriggerDispatcher 调度器测试"""

    def test_register_watchdog(self, tmp_path: Path):
        """register 根据 watchdog 策略创建 WatchdogTrigger"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src1",
            {"type": "watchdog", "events": ["modified"], "debounce": 1.5},
            tmp_path,
        )

        assert "src1" in dispatcher._triggers
        assert isinstance(dispatcher._triggers["src1"], WatchdogTrigger)
        assert dispatcher._triggers["src1"]._debounce == 1.5

    def test_register_polling(self, tmp_path: Path):
        """register 根据 polling 策略创建 PollingTrigger"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src2",
            {"type": "polling", "interval": 120, "pattern": "*.log"},
            tmp_path,
        )

        assert "src2" in dispatcher._triggers
        assert isinstance(dispatcher._triggers["src2"], PollingTrigger)
        assert dispatcher._triggers["src2"]._interval == 120
        assert dispatcher._triggers["src2"]._pattern == "*.log"

    def test_register_unknown_type_ignored(self, tmp_path: Path):
        """register 遇到未知触发类型时不创建触发器"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register("src3", {"type": "unknown"}, tmp_path)

        assert "src3" not in dispatcher._triggers

    def test_register_hybrid_pattern_forwarded(self, tmp_path: Path):
        """hybrid 策略的 pattern 应透传给 PollingTrigger"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src_hybrid",
            {"type": "hybrid", "interval": 60, "pattern": "*.db"},
            tmp_path,
        )
        assert "src_hybrid" in dispatcher._triggers
        trigger = dispatcher._triggers["src_hybrid"]
        assert isinstance(trigger, HybridTrigger)
        assert trigger._polling._pattern == "*.db"

    def test_start_all_calls_start_on_triggers(self, tmp_path: Path):
        """start_all 调用所有触发器的 start 方法"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src1", {"type": "watchdog", "events": ["modified"], "debounce": 1.0}, tmp_path
        )
        dispatcher.register(
            "src2", {"type": "polling", "interval": 60, "pattern": "*.txt"}, tmp_path
        )

        for trigger in dispatcher._triggers.values():
            trigger.start = MagicMock()

        dispatcher.start_all()

        for trigger in dispatcher._triggers.values():
            trigger.start.assert_called_once_with(tmp_path)

    def test_stop_all_calls_stop_on_triggers(self, tmp_path: Path):
        """stop_all 调用所有触发器的 stop 方法"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src1", {"type": "watchdog", "events": ["modified"], "debounce": 1.0}, tmp_path
        )

        for trigger in dispatcher._triggers.values():
            trigger.stop = MagicMock()

        dispatcher.stop_all()

        for trigger in dispatcher._triggers.values():
            trigger.stop.assert_called_once()

    def test_start_single_trigger(self, tmp_path: Path):
        """start(name) 只启动指定名称的触发器"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src1", {"type": "watchdog", "events": ["modified"], "debounce": 1.0}, tmp_path
        )
        dispatcher.register(
            "src2", {"type": "polling", "interval": 60, "pattern": "*.txt"}, tmp_path
        )

        for trigger in dispatcher._triggers.values():
            trigger.start = MagicMock()

        dispatcher.start("src1")

        dispatcher._triggers["src1"].start.assert_called_once()
        dispatcher._triggers["src2"].start.assert_not_called()

    def test_stop_single_trigger(self, tmp_path: Path):
        """stop(name) 只停止指定名称的触发器"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src1", {"type": "watchdog", "events": ["modified"], "debounce": 1.0}, tmp_path
        )
        dispatcher.register(
            "src2", {"type": "polling", "interval": 60, "pattern": "*.txt"}, tmp_path
        )

        for trigger in dispatcher._triggers.values():
            trigger.stop = MagicMock()

        dispatcher.stop("src2")

        dispatcher._triggers["src1"].stop.assert_not_called()
        dispatcher._triggers["src2"].stop.assert_called_once()

    def test_unregister_stops_and_removes_trigger(self, tmp_path: Path):
        """unregister(name) 注销指定触发器并清理路径表。"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)
        dispatcher.register(
            "src1", {"type": "watchdog", "events": ["modified"], "debounce": 1.0}, tmp_path
        )
        dispatcher.register(
            "src2", {"type": "polling", "interval": 60, "pattern": "*.txt"}, tmp_path
        )

        removed_trigger = dispatcher._triggers["src1"]
        removed_trigger.stop = MagicMock()
        dispatcher._triggers["src2"].stop = MagicMock()

        dispatcher.unregister("src1")

        assert "src1" not in dispatcher._triggers
        assert "src1" not in dispatcher._paths
        removed_trigger.stop.assert_called_once()
        assert "src2" in dispatcher._triggers
        assert "src2" in dispatcher._paths
        dispatcher._triggers["src2"].stop.assert_not_called()

    def test_unregister_missing_source_is_noop(self):
        """注销不存在的 source 应保持幂等，不抛异常。"""
        dispatcher = TriggerDispatcher(callback=lambda x: None)

        dispatcher.unregister("missing")

        assert dispatcher._triggers == {}
        assert dispatcher._paths == {}


# ---------------------------------------------------------------------------
# 错误隔离
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    """错误隔离测试：确保单个回调失败不影响系统整体运行"""

    def test_single_callback_failure_does_not_crash(self):
        """单个回调失败被捕获，不向外传播异常"""

        def bad_callback(_):
            raise RuntimeError("intentional failure")

        trigger = _ConcreteTrigger(callback=bad_callback, source_name="fragile")

        # 不应抛出异常
        trigger._execute_callback("/tmp/file1.txt")
        trigger._execute_callback("/tmp/file2.txt")

        assert trigger._error_count == 2

    def test_mixed_success_and_failure(self):
        """成功与失败交替：错误计数正确增减"""
        results = []

        def flaky_callback(path):
            results.append(path)
            if path == "fail":
                raise RuntimeError("boom")

        trigger = _ConcreteTrigger(callback=flaky_callback, source_name="flaky")

        trigger._execute_callback("ok1")
        assert trigger._error_count == 0

        trigger._execute_callback("fail")
        assert trigger._error_count == 1

        trigger._execute_callback("ok2")
        assert trigger._error_count == 0

        trigger._execute_callback("fail")
        assert trigger._error_count == 1

        assert results == ["ok1", "fail", "ok2", "fail"]

    def test_multiple_failures_backoff_increases(self):
        """连续失败导致退避延迟递增"""

        def always_fails(_):
            raise ValueError("nope")

        trigger = _ConcreteTrigger(callback=always_fails, source_name="broken")

        trigger._execute_callback("a")
        assert trigger._error_count == 1
        assert trigger._backoff_delay() == 10

        trigger._execute_callback("b")
        assert trigger._error_count == 2
        assert trigger._backoff_delay() == 20

        trigger._execute_callback("c")
        assert trigger._error_count == 3
        assert trigger._backoff_delay() == 40

    def test_success_after_failure_reduces_backoff(self):
        """失败后成功，错误计数下降，退避延迟减少"""

        def toggle_callback(path):
            if path == "fail":
                raise RuntimeError("boom")

        trigger = _ConcreteTrigger(callback=toggle_callback, source_name="recover")
        trigger._error_count = 4  # 当前退避应为 80s

        trigger._execute_callback("fail")
        assert trigger._error_count == 5
        assert trigger._backoff_delay() == 160

        trigger._execute_callback("ok")
        assert trigger._error_count == 4
        assert trigger._backoff_delay() == 80

        trigger._execute_callback("ok")
        assert trigger._error_count == 3
        assert trigger._backoff_delay() == 40
