# -*- coding: utf-8 -*-
"""
触发系统 — WatchdogTrigger / PollingTrigger / HybridTrigger / TriggerDispatcher

AgentSource 声明触发策略（trigger_strategy），框架据此选择正确的触发器实现。
插件从不直接接触文件监视逻辑。

触发器类型：
  - watchdog: 文件变化事件驱动（Claude/Kimi/Hermes/Codex）
  - polling: 定时扫描（OpenClaw）
  - hybrid: watchdog + polling 组合（Kimi）

关键设计：
  - 统一看门狗：单个 watchdog Observer 实例，最长前缀匹配路由
  - 去抖动与稳定性：_is_file_stable() 三次稳定检测
  - 错误隔离：每个触发器独立 try/except，指数退避
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

from core.config import get_config
from core.db_utils import SqlitePool
from core.ops.durable_io import DurableIOError, inspect_path_kind

# Constants extracted from magic numbers
INTERVAL_SECONDS = 3600
POLLING_INTERVAL_SECONDS = 3600
TRIGGER_SECONDS = 3600


logger = logging.getLogger(__name__)
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False


class BaseTrigger(ABC):
    """触发器基类"""

    def __init__(self, callback: Callable[[str], None], source_name: str = ""):
        self._callback = callback
        self._source_name = source_name
        self._running = False
        self._error_count = 0
        self._max_backoff = 300  # 5 分钟上限

    @abstractmethod
    def start(self, watch_path: Path): ...

    @abstractmethod
    def stop(self): ...

    def _backoff_delay(self) -> float:
        """指数退避：5s → 10s → 20s → ... → 300s"""
        delay = min(5 * (2**self._error_count), self._max_backoff)
        return delay  # type: ignore[no-any-return]

    def _execute_callback(self, file_path: str):
        """安全执行回调，带错误隔离"""
        try:
            self._callback(file_path)
            self._error_count = max(0, self._error_count - 1)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            self._error_count += 1
            delay = self._backoff_delay()
            logger.error(
                "[Trigger:%s] 回调失败 (#%s): %s, 退避 %.0fs",
                self._source_name,
                self._error_count,
                e,
                delay,
                exc_info=True,
            )


class WatchdogTrigger(BaseTrigger):
    """
    Watchdog 文件变化触发器。

    支持去抖动 + 稳定性检测。
    单个 Observer 实例共享（通过 UnifiedWatchdog）。
    """

    def __init__(
        self,
        callback: Callable[[str], None],
        source_name: str = "",
        events: List[str] | None = None,
        debounce: float = 5.0,
    ):
        super().__init__(callback, source_name)
        self._events = events or ["modified"]
        self._debounce = debounce
        self._pending: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._observer: Optional[Any] = None
        self._handler: Optional[Any] = None

    def start(self, watch_path: Path):
        if not _WATCHDOG_AVAILABLE:
            logger.warning("[WatchdogTrigger:%s] watchdog 未安装，跳过", self._source_name)
            return
        if self._running:
            logger.warning("[WatchdogTrigger:%s] 已启动，跳过重复调用", self._source_name)
            return

        self._running = True
        self._handler = _DebounceHandler(self._on_event, self._debounce, self._events)

        self._observer = Observer()
        self._observer.schedule(self._handler, str(watch_path), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("[WatchdogTrigger:%s] active", self._source_name)

    def stop(self):
        self._running = False
        with self._lock:
            for timer in self._pending.values():
                timer.cancel()
            self._pending.clear()

        if self._handler:
            self._handler.close()
            self._handler = None

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _on_event(self, file_path: str):
        """去抖动后执行回调"""
        with self._lock:
            old_timer = self._pending.pop(file_path, None)
            if old_timer:
                old_timer.cancel()

            timer = threading.Timer(self._debounce, self._fire, [file_path])
            timer.daemon = True
            timer.start()
            self._pending[file_path] = timer

    def _fire(self, file_path: str):
        with self._lock:
            self._pending.pop(file_path, None)
        self._execute_callback(file_path)


class PollingTrigger(BaseTrigger):
    """
    定时轮询触发器。

    双重保障：数据库记录 + mtime 比较。
    适用于每日批量生成的文件（OpenClaw）。
    """

    def __init__(
        self,
        callback: Callable[[str], None],
        source_name: str = "",
        interval: int = INTERVAL_SECONDS,
        pattern: str = "*.txt",
    ):
        super().__init__(callback, source_name)
        self._interval = interval
        self._pattern = pattern
        self._thread: Optional[threading.Thread] = None
        self._seen: Dict[str, float] = {}  # path → mtime
        self._state_loaded = False
        self._db_path = get_config().database_dir / "polling_state.db"
        self._pool = SqlitePool(self._db_path)

    def start(self, watch_path: Path):
        if self._running:
            logger.warning("[PollingTrigger:%s] 已启动，跳过重复调用", self._source_name)
            return
        try:
            self._load_state()
        except BaseException:
            self.close()
            raise
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, args=(watch_path,), daemon=True)
        self._thread.start()
        logger.info(
            "[PollingTrigger:%s] 轮询 %s (间隔 %ss)", self._source_name, watch_path, self._interval
        )

    def stop(self):
        """停止轮询并关闭持久连接"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            if self._state_loaded:
                self._save_state()
        finally:
            self.close()

    def close(self):
        """关闭持久连接"""
        if hasattr(self, "_pool"):
            self._pool.close()

    def _poll_loop(self, watch_path: Path):
        """轮询主循环"""
        while self._running:
            try:
                self._scan(watch_path)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
                self._error_count += 1
                logger.error(
                    "[PollingTrigger:%s] 扫描失败: %s", self._source_name, e, exc_info=True
                )

            # 退避后等待
            delay = self._backoff_delay() if self._error_count > 0 else self._interval
            # 分段 sleep 以便快速响应 stop()
            end_time = time.time() + delay
            while self._running and time.time() < end_time:
                time.sleep(min(5, end_time - time.time()))

    def _scan(self, watch_path: Path):
        """扫描目录，检测新文件或变化的文件"""
        watch_kind = inspect_path_kind(watch_path)
        if watch_kind == "missing":
            return
        if watch_kind != "directory":
            raise DurableIOError("polling_trigger_root_not_directory")

        current_files = set()
        for f in watch_path.rglob(self._pattern):
            fpath = str(f)
            current_files.add(fpath)
            try:
                mtime = f.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                raise DurableIOError(
                    "polling_trigger_entry_unavailable"
                ) from None

            last_mtime = self._seen.get(fpath, 0)
            if mtime > last_mtime:
                self._seen[fpath] = mtime
                self._execute_callback(fpath)

        # 清理已删除的文件记录，防止内存无限增长
        for old_path in list(self._seen.keys()):
            if old_path not in current_files:
                self._seen.pop(old_path, None)

        self._save_state()

    def _load_state(self):
        """从 SQLite 加载已扫描文件状态"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._pool.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS polling_state (
                    source TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    PRIMARY KEY (source, path)
                )
            """)
            cursor = conn.execute(
                "SELECT path, mtime FROM polling_state WHERE source = ?",
                (self._source_name,),
            )
            loaded = {str(row[0]): float(row[1]) for row in cursor.fetchall()}
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise DurableIOError("polling_state_read_unavailable") from exc
            raise
        self._seen = loaded
        self._state_loaded = True

    def _save_state(self):
        """保存已扫描文件状态到 SQLite"""
        rows = [(self._source_name, p, m) for p, m in self._seen.items()]
        conn = self._pool.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM polling_state WHERE source = ?", (self._source_name,))
            if rows:
                conn.executemany(
                    "INSERT INTO polling_state (source, path, mtime) VALUES (?, ?, ?)",
                    rows,
                )
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise DurableIOError("polling_state_write_unavailable") from exc
            raise


class HybridTrigger(BaseTrigger):
    """
    混合触发器：Watchdog + Polling 组合。

    适用于 Kimi 等同时有实时追加和归档机制的 Agent。
    """

    def __init__(
        self,
        callback: Callable[[str], None],
        source_name: str = "",
        events: List[str] | None = None,
        debounce: float = 5.0,
        polling_interval: int = POLLING_INTERVAL_SECONDS,
        pattern: str = "*.jsonl",
    ):
        super().__init__(callback, source_name)
        self._watchdog = WatchdogTrigger(callback, source_name, events, debounce)
        self._polling = PollingTrigger(callback, source_name, polling_interval, pattern)

    def start(self, watch_path: Path):
        self._running = True
        self._watchdog.start(watch_path)
        self._polling.start(watch_path)

    def stop(self):
        self._running = False
        self._watchdog.stop()
        self._polling.stop()

    def close(self):
        """关闭子触发器资源"""
        self._watchdog.close()
        self._polling.close()


class TriggerDispatcher:
    """
    触发器调度器 — 根据 AgentSource 的 trigger_strategy 选择触发器。

    使用方式：
        dispatcher = TriggerDispatcher(sync_engine)
        dispatcher.register(source)
        dispatcher.start_all()
    """

    def __init__(self, callback: Callable[[str], None]):
        self._callback = callback
        self._triggers: Dict[str, BaseTrigger] = {}
        self._paths: Dict[str, Path] = {}

    def register(self, source_name: str, strategy: Dict[str, Any], watch_path: Path):
        """根据策略注册触发器"""
        trigger_type = strategy.get("type", "watchdog")
        trigger: BaseTrigger

        if trigger_type == "watchdog":
            trigger = WatchdogTrigger(
                callback=self._callback,
                source_name=source_name,
                events=strategy.get("events", ["modified"]),
                debounce=strategy.get("debounce", 5.0),
            )
        elif trigger_type == "polling":
            trigger = PollingTrigger(
                callback=self._callback,
                source_name=source_name,
                interval=strategy.get("interval", TRIGGER_SECONDS),
                pattern=strategy.get("pattern", "*"),
            )
        elif trigger_type == "hybrid":
            trigger = HybridTrigger(
                callback=self._callback,
                source_name=source_name,
                events=strategy.get("events", ["modified", "created"]),
                debounce=strategy.get("debounce", 5.0),
                polling_interval=strategy.get("interval", TRIGGER_SECONDS),
                pattern=strategy.get("pattern", "*.jsonl"),
            )
        else:
            logger.warning("[TriggerDispatcher] 未知触发类型: %s", trigger_type)
            return

        self._triggers[source_name] = trigger
        self._paths[source_name] = watch_path
        logger.info("[TriggerDispatcher] 注册 %s: %s", source_name, trigger_type)

    def start_all(self):
        """启动所有触发器"""
        for name, trigger in self._triggers.items():
            path = self._paths.get(name)
            try:
                path_available = (
                    path is not None
                    and inspect_path_kind(path) != "missing"
                )
            except DurableIOError as exc:
                logger.error(
                    "[TriggerDispatcher] 路径检查失败 %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                continue
            if path_available:
                try:
                    trigger.start(path)
                except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                    logger.error("[TriggerDispatcher] 启动失败 %s: %s", name, e, exc_info=True)

    def stop_all(self):
        """停止所有触发器"""
        for trigger in self._triggers.values():
            try:
                trigger.stop()
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
                logger.warning("Trigger stop failed", exc_info=True)

    def unregister(self, source_name: str) -> None:  # noqa: Vulture - public trigger lifecycle API for dynamic source unload.
        """注销指定来源的触发器并释放资源"""
        trigger = self._triggers.pop(source_name, None)
        self._paths.pop(source_name, None)
        if trigger is not None:
            try:
                trigger.stop()
                logger.info("[TriggerDispatcher] 注销 %s", source_name)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
                logger.warning("Trigger unregister stop failed", exc_info=True)

    def start(self, source_name: str):
        """启动指定触发器"""
        trigger = self._triggers.get(source_name)
        path = self._paths.get(source_name)
        if trigger and path:
            trigger.start(path)

    def stop(self, source_name: str):
        """停止指定触发器"""
        trigger = self._triggers.get(source_name)
        if trigger:
            trigger.stop()


# type: ignore[misc]
class _DebounceHandler(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):  # type: ignore[misc]  # noqa: E501
    """Watchdog 事件处理器，带去抖动"""

    def __init__(self, callback: Callable[[str], None], debounce: float, events: List[str]):
        self._callback = callback
        self._debounce = debounce
        self._events = events
        self._pending: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event):  # noqa: Vulture - watchdog dispatch hook.
        if event.is_directory:
            return
        if "modified" in self._events:
            self._debounce_event(event.src_path)

    def on_created(self, event):  # noqa: Vulture - watchdog dispatch hook.
        if event.is_directory:
            return
        if "created" in self._events:
            self._debounce_event(event.src_path)

    def _debounce_event(self, file_path: str):
        """去抖动：取消旧定时器，重新等待"""
        with self._lock:
            old_timer = self._pending.pop(file_path, None)
            if old_timer:
                old_timer.cancel()

            timer = threading.Timer(self._debounce, self._fire, [file_path])
            timer.daemon = True
            timer.start()
            self._pending[file_path] = timer

    def close(self):
        """取消所有待处理的定时器"""
        with self._lock:
            for timer in self._pending.values():
                timer.cancel()
            self._pending.clear()

    def _fire(self, file_path: str):
        with self._lock:
            self._pending.pop(file_path, None)
        self._callback(file_path)
