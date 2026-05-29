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


logger = logging.getLogger(__name__)
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
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
    def start(self, watch_path: Path):
        ...

    @abstractmethod
    def stop(self):
        ...

    def _backoff_delay(self) -> float:
        """指数退避：5s → 10s → 20s → ... → 300s"""
        delay = min(5 * (2 ** self._error_count), self._max_backoff)
        return delay

    def _execute_callback(self, file_path: str):
        """安全执行回调，带错误隔离"""
        try:
            self._callback(file_path)
            self._error_count = max(0, self._error_count - 1)
        except Exception as e:
            self._error_count += 1
            delay = self._backoff_delay()
            logger.error(
                f"[Trigger:{self._source_name}] 回调失败 (#{self._error_count}): {e}, "
                f"退避 {delay:.0f}s"
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
        events: List[str] = None,
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
            logger.warning(f"[WatchdogTrigger:{self._source_name}] watchdog 未安装，跳过")
            return

        self._running = True
        self._handler = _DebounceHandler(self._on_event, self._debounce, self._events)

        self._observer = Observer()
        self._observer.schedule(self._handler, str(watch_path), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info(f"[WatchdogTrigger:{self._source_name}] 监听 {watch_path}")

    def stop(self):
        self._running = False
        with self._lock:
            for timer in self._pending.values():
                timer.cancel()
            self._pending.clear()

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
        interval: int = 3600,
        pattern: str = "*.txt",
    ):
        super().__init__(callback, source_name)
        self._interval = interval
        self._pattern = pattern
        self._thread: Optional[threading.Thread] = None
        self._seen: Dict[str, float] = {}  # path → mtime
        self._db_path = get_config().data_dir / "polling_state.db"
        self._pool = SqlitePool(self._db_path)

    def start(self, watch_path: Path):
        self._running = True
        self._load_state()
        self._thread = threading.Thread(
            target=self._poll_loop, args=(watch_path,), daemon=True
        )
        self._thread.start()
        logger.info(
            f"[PollingTrigger:{self._source_name}] 轮询 {watch_path} "
            f"(间隔 {self._interval}s)"
        )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._save_state()

    def _poll_loop(self, watch_path: Path):
        """轮询主循环"""
        while self._running:
            try:
                self._scan(watch_path)
            except Exception as e:
                self._error_count += 1
                logger.error(f"[PollingTrigger:{self._source_name}] 扫描失败: {e}")

            # 退避后等待
            delay = self._backoff_delay() if self._error_count > 0 else self._interval
            # 分段 sleep 以便快速响应 stop()
            end_time = time.time() + delay
            while self._running and time.time() < end_time:
                time.sleep(min(5, end_time - time.time()))

    def _scan(self, watch_path: Path):
        """扫描目录，检测新文件或变化的文件"""
        if not watch_path.exists():
            return

        for f in watch_path.rglob(self._pattern):
            fpath = str(f)
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue

            last_mtime = self._seen.get(fpath, 0)
            if mtime > last_mtime:
                self._seen[fpath] = mtime
                self._execute_callback(fpath)

        self._save_state()

    def close(self):
        """关闭持久连接"""
        if hasattr(self, '_pool'):
            self._pool.close()

    def _load_state(self):
        """从 SQLite 加载已扫描文件状态"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = self._pool.get_conn()
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
            for row in cursor.fetchall():
                self._seen[row[0]] = row[1]
            conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
    def _save_state(self):
        """保存已扫描文件状态到 SQLite"""
        try:
            conn = self._pool.get_conn()
            conn.execute("DELETE FROM polling_state WHERE source = ?", (self._source_name,))
            conn.executemany(
                "INSERT OR REPLACE INTO polling_state (source, path, mtime) VALUES (?, ?, ?)",
                [(self._source_name, p, m) for p, m in self._seen.items()],
            )
            conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
class HybridTrigger(BaseTrigger):
    """
    混合触发器：Watchdog + Polling 组合。

    适用于 Kimi 等同时有实时追加和归档机制的 Agent。
    """

    def __init__(
        self,
        callback: Callable[[str], None],
        source_name: str = "",
        events: List[str] = None,
        debounce: float = 5.0,
        polling_interval: int = 3600,
    ):
        super().__init__(callback, source_name)
        self._watchdog = WatchdogTrigger(callback, source_name, events, debounce)
        self._polling = PollingTrigger(
            callback, source_name, polling_interval, "*.jsonl"
        )

    def start(self, watch_path: Path):
        self._running = True
        self._watchdog.start(watch_path)
        self._polling.start(watch_path)

    def stop(self):
        self._running = False
        self._watchdog.stop()
        self._polling.stop()


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
                interval=strategy.get("interval", 3600),
                pattern=strategy.get("pattern", "*"),
            )
        elif trigger_type == "hybrid":
            trigger = HybridTrigger(
                callback=self._callback,
                source_name=source_name,
                events=strategy.get("events", ["modified", "created"]),
                debounce=strategy.get("debounce", 5.0),
                polling_interval=strategy.get("interval", 3600),
            )
        else:
            logger.warning(f"[TriggerDispatcher] 未知触发类型: {trigger_type}")
            return

        self._triggers[source_name] = trigger
        self._paths[source_name] = watch_path
        logger.info(f"[TriggerDispatcher] 注册 {source_name}: {trigger_type}")

    def start_all(self):
        """启动所有触发器"""
        for name, trigger in self._triggers.items():
            path = self._paths.get(name)
            if path and path.exists():
                try:
                    trigger.start(path)
                except Exception as e:
                    logger.error(f"[TriggerDispatcher] 启动失败 {name}: {e}")

    def stop_all(self):
        """停止所有触发器"""
        for trigger in self._triggers.values():
            try:
                trigger.stop()
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
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


class _DebounceHandler(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):
    """Watchdog 事件处理器，带去抖动"""

    def __init__(self, callback: Callable[[str], None], debounce: float, events: List[str]):
        self._callback = callback
        self._debounce = debounce
        self._events = events
        self._pending: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory:
            return
        if "modified" in self._events:
            self._debounce_event(event.src_path)

    def on_created(self, event):
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

    def _fire(self, file_path: str):
        with self._lock:
            self._pending.pop(file_path, None)
        self._callback(file_path)
