# Mnemos Event Bus — 统一事件总线
#
# 职责：
# - 提供跨 Agent / 跨进程的事件通信机制
# - 基于 SQLite + in-memory queue（高可靠、低延迟）
# - 所有 Agent 适配器通过此总线发布和消费事件
# - Daemon 统一轮询并分发处理
#
# 设计原则：
# - Agent-Agnostic：事件格式不感知 Agent 类型
# - At-least-once delivery：事件先持久化到 SQLite，再投递
# - 可靠：失败自动重试，超限进死信队列
# - 向后兼容：保留旧文件系统接口（标记 deprecated）

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Union

from core.config import get_config

logger = logging.getLogger(__name__)

# 事件目录根路径（旧文件系统方式，保留兼容）
EVENTS_ROOT = Path.home() / ".mnemos" / "events"

# ============================================================
# 蓝图定义的全部事件类型 (00-接口契约 §12)
# ============================================================
EVENT_TYPES = [
    "memory_synced", "content_scored", "knowledge_distilled",
    "entity_discovered", "relation_conflicted", "profile_updated",
    "blind_spot_detected", "dispute_created", "system_alert",
    "wiki_search_requested", "distillation_progress", "distill_complete",
    "knowledge.ingested", "scheduler.daily",
    "session.start", "session.end", "distill.request", "signal.batch",
]


# ============================================================
# Event 数据类
# ============================================================

@dataclass
class Event:
    """标准事件格式"""

    event_type: str
    source: str
    payload: Dict[str, Any]
    trace_id: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = str(uuid.uuid4())[:16]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Optional["Event"]:
        """从 SQLite 行反序列化"""
        try:
            return cls(
                event_type=row["event_type"],
                source=row["source"],
                payload=json.loads(row["payload_json"]),
                trace_id=row["trace_id"],
                timestamp=row["timestamp"],
            )
        except Exception as e:
            logger.warning(f"从数据库行反序列化事件失败: {e}")
            return None

    @classmethod
    def from_file(cls, path: Path) -> Optional["Event"]:
        """从事件文件反序列化（旧格式兼容）"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # 旧格式字段映射
            return cls(
                event_type=data.get("event_type", ""),
                source=data.get("agent", data.get("source", "")),
                payload=data.get("payload", {}),
                trace_id=data.get("event_id", data.get("trace_id", "")),
                timestamp=data.get("timestamp", ""),
            )
        except Exception as e:
            logger.warning(f"读取事件文件失败 {path}: {e}")
            return None

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# ============================================================
# EventBus — SQLite + in-memory queue 实现
# ============================================================

class EventBus:
    """统一事件总线

    基于 SQLite + in-memory queue 的事件系统：
    - SQLite: 持久化事件，保证 at-least-once delivery
    - queue.Queue: 内存队列，低延迟分发
    - 启动时恢复 pending/processing 事件
    - 失败重试 + 死信队列
    """

    # SQLite 表结构
    _SCHEMA_EVENTS = """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
    """

    _SCHEMA_DEAD_LETTERS = """
        CREATE TABLE IF NOT EXISTS dead_letters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'dead',
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            failure_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_dead_letters_type ON dead_letters(event_type);
    """

    # 全局单例锁
    _instance_lock = threading.Lock()

    def __init__(self, root_dir: Optional[Path] = None):
        # 配置
        config = get_config()
        self._mnemos_dir = config.mnemos_dir
        self._max_retries = config.get("event_bus.max_retries", 5)
        self._queue_depth_alert = config.get("event_bus.queue_depth_alert", 1000)
        self._max_queue_depth = config.get("event_bus.max_queue_depth", 10000)
        self._max_recover_events = config.get("event_bus.max_recover_events", 1000)
        self._dead_letter_alert = config.get("event_bus.dead_letter_alert", 10)
        self._max_latency_ms = config.get("event_bus.max_latency_ms", 10)

        # SQLite
        self._db_path = self._mnemos_dir / "events.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()  # 每线程独立连接
        self._all_conns: set = set()      # 追踪所有创建的连接，用于 close() 统一清理
        self._conns_lock = threading.Lock()
        self._init_db()

        # In-memory queue
        self._queue: queue.Queue = queue.Queue()

        # 订阅者
        self._handlers: Dict[str, List[Callable[[Event], Any]]] = {}
        self._handlers_lock = threading.Lock()

        # 分发线程
        self._dispatch_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 旧文件系统路径（向后兼容）
        self.root = root_dir or EVENTS_ROOT
        self._ensure_dirs()

        # 启动时恢复未完成事件
        self._recover_pending()

    # ---- SQLite 连接管理 ----

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.add(conn)
        return self._local.conn

    def _init_db(self):
        """初始化数据库表并清理过期数据"""
        conn = self._get_conn()
        conn.executescript(self._SCHEMA_EVENTS)
        conn.executescript(self._SCHEMA_DEAD_LETTERS)
        conn.commit()
        # 启动时清理旧数据，防止表无限增长
        try:
            conn.execute(
                "DELETE FROM events WHERE status = 'done' AND created_at < datetime('now', '-7 days')"
            )
            conn.execute(
                "DELETE FROM dead_letters WHERE timestamp < datetime('now', '-30 days')"
            )
            conn.execute(
                "DELETE FROM events WHERE status = 'pending' AND created_at < datetime('now', '-3 days')"
            )
            conn.commit()
        except Exception:
            pass

    def close(self):
        """关闭所有线程的数据库连接"""
        with self._conns_lock:
            for conn in list(self._all_conns):
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                logger.warning(f"Unexpected error in mnemos_bus.py", exc_info=True)
                pass
            self._local.conn = None

    # ---- 启动恢复 ----

    def _recover_pending(self):
        """启动时恢复 pending 和 processing 事件到内存队列"""
        conn = self._get_conn()
        total_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE status IN ('pending', 'processing')"
        ).fetchone()
        total = total_row["cnt"] if total_row else 0
        limit = int(self._max_recover_events or 1000)
        cursor = conn.execute(
            """SELECT * FROM events
               WHERE status IN ('pending', 'processing')
               ORDER BY id ASC
               LIMIT ?""",
            (limit,),
        )
        rows = cursor.fetchall()
        for i, row in enumerate(rows):
            event = Event.from_row(row)
            if event:
                self._queue.put(event)
                # processing 状态重置为 pending（crash recovery）
                conn.execute(
                    "UPDATE events SET status = 'pending' WHERE id = ?", (row["id"],)
                )
                # 每 100 条 commit 一次，减少锁持有时间
                if (i + 1) % 100 == 0:
                    conn.commit()
        conn.commit()
        if rows:
            logger.info(f"[EventBus] 恢复 {len(rows)} 个未完成事件")
        if total > limit:
            logger.warning(
                "[EventBus] 未完成事件积压 %d 个，本次仅恢复 %d 个，"
                "剩余事件保留 pending，后续分批恢复",
                total, limit,
            )

    # ---- 目录管理（旧文件系统兼容） ----

    def _ensure_dirs(self):
        """确保事件目录结构存在"""
        for sub in ["inbox", "processing", "archive"]:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ========== 发布事件 ==========

    def publish(
        self,
        event: Union[Event, str],
        payload: Optional[Dict[str, Any]] = None,
        agent: Optional[str] = None,
    ) -> str:
        """发布事件

        支持两种调用风格：
        1. 新风格：publish(event_obj) 或 publish("event_type", payload={...})
        2. 旧风格：publish("event_type", agent="claude", payload={...})

        旧风格签名 publish(event_type, agent, payload) 也仍然支持：
          publish("session.start", "claude", {"key": "val"})
          会被解释为 event="session.start", agent="claude",
          此时 payload 参数位置是第二个位置参数。

        为保持完全向后兼容，当第二个位置参数是 dict 时，
        视为 payload；当是 str 时，视为 agent。

        Returns:
            trace_id
        """
        # ---- 向后兼容处理 ----
        # 旧签名: publish(event_type: str, agent: str, payload: Dict)
        # 新签名: publish(event: Union[Event, str], payload: Optional[Dict])
        # 策略：如果 event 是 str，且 payload 也是 str，说明是旧调用
        if isinstance(event, str):
            if isinstance(payload, str):
                # 旧风格：publish(event_type, agent, payload_dict)
                # payload 实际是 agent 名，agent 参数位置是真正的 payload
                old_agent = payload
                old_payload = agent if isinstance(agent, dict) else {}
                event = Event(event_type=event, source=old_agent, payload=old_payload)
            elif agent is not None and isinstance(agent, dict):
                # 新风格但用关键字: publish("type", payload=None, agent={...}) 不太合理
                # 这种情况不太可能出现，按新风格处理
                event = Event(event_type=event, source="", payload=payload or {})
            elif agent is not None and isinstance(agent, str):
                # 新风格但指定了 source: publish("type", payload=..., agent="source")
                event = Event(event_type=event, source=agent, payload=payload or {})
            else:
                # 标准新风格: publish("type", payload={...})
                event = Event(event_type=event, source="", payload=payload or {})

        # ---- 持久化到 SQLite ----
        trace_id = event.trace_id
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO events (timestamp, trace_id, event_type, source, payload_json, status, retry_count, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)""",
            (
                event.timestamp,
                event.trace_id,
                event.event_type,
                event.source,
                json.dumps(event.payload, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()

        qsize = self._queue.qsize()
        if qsize >= self._max_queue_depth:
            logger.warning(
                "[EventBus] 内存队列深度 %d 已达上限 %d，事件已持久化但暂不入内存队列",
                qsize, self._max_queue_depth,
            )
            return trace_id

        # ---- 推入内存队列 ----
        self._queue.put(event)

        # 检查队列深度告警
        qsize = self._queue.qsize()
        if qsize > self._queue_depth_alert:
            logger.warning(
                f"[EventBus] 队列深度 {qsize} 超过告警阈值 {self._queue_depth_alert}"
            )

        logger.info(
            f"[EventBus] 发布事件: {event.event_type} from {event.source} trace_id={trace_id}"
        )
        return trace_id

    # ========== 订阅事件 ==========

    def subscribe(self, event_type: str, handler: Callable[[Event], Any]):
        """注册事件处理器

        Args:
            event_type: 事件类型（支持通配符 "*"）
            handler: 处理函数，接受 Event 参数
        """
        with self._handlers_lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)
        logger.info(f"[EventBus] 订阅: {event_type} -> {handler.__name__}")

    # ========== 分发循环 ==========

    def start_dispatch(self):
        """启动后台分发线程"""
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            return
        self._stop_event.clear()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="EventBus-Dispatch"
        )
        self._dispatch_thread.start()
        logger.info("[EventBus] 分发线程已启动")

    def stop_dispatch(self):
        """停止分发线程"""
        self._stop_event.set()
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=5)
        logger.info("[EventBus] 分发线程已停止")

    def _dispatch_loop(self):
        """后台分发循环：从内存队列取事件，调用处理器"""
        while not self._stop_event.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            self._dispatch_event(event)
            self._queue.task_done()

    def _dispatch_event(self, event: Event):
        """分发单个事件到对应处理器"""
        # 查找处理器
        handlers: List[Callable[[Event], Any]] = []
        with self._handlers_lock:
            handlers.extend(self._handlers.get(event.event_type, []))
            handlers.extend(self._handlers.get("*", []))  # 通配符

        if not handlers:
            logger.debug(f"[EventBus] 无处理器: {event.event_type}")
            # 无处理器时仍然标记完成（避免堆积）
            self._mark_done(event.trace_id)
            return

        # 调用处理器
        all_ok = True
        failure_reasons = []
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                all_ok = False
                failure_reasons.append(f"{handler.__name__}: {e}")
                logger.error(
                    f"[EventBus] 处理器 {handler.__name__} 处理事件 "
                    f"{event.event_type} 失败: {e}"
                )

        if all_ok:
            self._mark_done(event.trace_id)
        else:
            self._mark_failed(event.trace_id, "; ".join(failure_reasons))

    # ---- SQLite 状态更新 ----

    def _mark_done(self, trace_id: str):
        """标记事件为 done"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE events SET status = 'done' WHERE trace_id = ?", (trace_id,)
        )
        conn.commit()

    def _mark_failed(self, trace_id: str, reason: str = ""):
        """标记事件失败：递增 retry_count，超限则移入死信队列"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, retry_count FROM events WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        if not row:
            return

        new_retry = row["retry_count"] + 1
        if new_retry >= self._max_retries:
            # 移入死信队列
            conn.execute(
                """INSERT INTO dead_letters (timestamp, trace_id, event_type, source, payload_json, status, retry_count, created_at, failure_reason)
                   SELECT timestamp, trace_id, event_type, source, payload_json, 'dead', retry_count + 1, created_at, ?
                   FROM events WHERE trace_id = ?""",
                (reason, trace_id),
            )
            conn.execute("DELETE FROM events WHERE trace_id = ?", (trace_id,))
            conn.commit()

            # 检查死信队列告警并清理旧数据
            dl_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM dead_letters"
            ).fetchone()["cnt"]
            if dl_count > self._dead_letter_alert:
                logger.warning(
                    f"[EventBus] 死信队列数量 {dl_count} 超过告警阈值 "
                    f"{self._dead_letter_alert}"
                )
            if dl_count > self._dead_letter_max:
                # 保留最新的死信，删除旧的
                conn.execute(
                    """DELETE FROM dead_letters WHERE id IN (
                        SELECT id FROM dead_letters ORDER BY id ASC LIMIT ?
                    )""",
                    (dl_count - self._dead_letter_max,),
                )
                conn.commit()
                logger.info(f"[EventBus] 死信队列清理完成，保留 {self._dead_letter_max} 条")
            logger.warning(
                f"[EventBus] 事件 {trace_id} 重试 {new_retry} 次后移入死信队列"
            )
        else:
            # 重置为 pending，重新入队
            conn.execute(
                "UPDATE events SET status = 'pending', retry_count = ? WHERE trace_id = ?",
                (new_retry, trace_id),
            )
            conn.commit()
            # 重新推入内存队列等待下次处理
            row2 = conn.execute(
                "SELECT * FROM events WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if row2:
                event = Event.from_row(row2)
                if event:
                    self._queue.put(event)
            logger.info(
                f"[EventBus] 事件 {trace_id} 重试 {new_retry}/{self._max_retries}"
            )

    # ========== 查询接口 ==========

    def get_dead_letters(
        self, event_type: Optional[str] = None, limit: int = 100
    ) -> List[Dict]:
        """查询死信队列"""
        conn = self._get_conn()
        if event_type:
            cursor = conn.execute(
                "SELECT * FROM dead_letters WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM dead_letters ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in cursor.fetchall()]

    def replay_dead_letter(self, trace_id: str) -> bool:
        """将死信事件重新放回事件队列"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        if not row:
            return False

        event = Event.from_row(row)
        if not event:
            return False

        # 重新插入 events 表
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO events (timestamp, trace_id, event_type, source, payload_json, status, retry_count, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)""",
            (
                event.timestamp,
                event.trace_id,
                event.event_type,
                event.source,
                json.dumps(event.payload, ensure_ascii=False),
                now,
            ),
        )
        conn.execute("DELETE FROM dead_letters WHERE trace_id = ?", (trace_id,))
        conn.commit()

        # 推入内存队列
        self._queue.put(event)
        logger.info(f"[EventBus] 重放死信事件: {trace_id}")
        return True

    # ========== 统计信息 ==========

    def stats(self) -> Dict[str, int]:
        """返回各状态事件数量"""
        conn = self._get_conn()
        result = {}
        for status in ("pending", "processing", "done"):
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM events WHERE status = ?", (status,)
            ).fetchone()
            result[status] = row["cnt"]

        # 死信队列
        dl_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM dead_letters"
        ).fetchone()
        result["dead_letters"] = dl_row["cnt"]

        # 内存队列
        result["queue_depth"] = self._queue.qsize()

        return result

    # ========== 旧文件系统接口（deprecated） ==========

    def poll(self, event_types: Optional[List[str]] = None, limit: int = 100) -> List[Event]:
        """轮询 inbox 中的待处理事件（已废弃，使用 subscribe + start_dispatch）

        .. deprecated::
            请使用 subscribe() 注册处理器 + start_dispatch() 启动分发。
        """
        warnings.warn(
            "EventBus.poll() is deprecated, use subscribe() + start_dispatch()",
            DeprecationWarning,
            stacklevel=2,
        )
        conn = self._get_conn()
        if event_types:
            placeholders = ",".join("?" * len(event_types))
            cursor = conn.execute(
                f"SELECT * FROM events WHERE status = 'pending' AND event_type IN ({placeholders}) ORDER BY id ASC LIMIT ?",
                (*event_types, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM events WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                (limit,),
            )
        return [Event.from_row(row) for row in cursor.fetchall() if Event.from_row(row)]

    def ack(self, event_id: str) -> bool:
        """确认事件已处理（已废弃）

        .. deprecated::
            事件现在自动确认，无需手动 ack。
        """
        warnings.warn(
            "EventBus.ack() is deprecated, events are auto-acknowledged",
            DeprecationWarning,
            stacklevel=2,
        )
        conn = self._get_conn()
        # 兼容旧 event_id（现在用 trace_id）
        cursor = conn.execute(
            "SELECT trace_id FROM events WHERE trace_id = ? OR id = ?",
            (event_id, event_id),
        )
        row = cursor.fetchone()
        if not row:
            logger.warning(f"[EventBus] 未找到事件: {event_id}")
            return False
        self._mark_done(row["trace_id"])
        logger.info(f"[EventBus] 归档事件: {event_id}")
        return True

    def move_to_processing(self, event_id: str) -> bool:
        """将事件移到 processing 状态（已废弃）

        .. deprecated::
            事件状态现在由分发循环自动管理。
        """
        warnings.warn(
            "EventBus.move_to_processing() is deprecated, status is managed automatically",
            DeprecationWarning,
            stacklevel=2,
        )
        conn = self._get_conn()
        conn.execute(
            "UPDATE events SET status = 'processing' WHERE trace_id = ? OR id = ?",
            (event_id, event_id),
        )
        conn.commit()
        return True


# ============================================================
# Event Processor — 事件处理器（Daemon 使用，保留接口兼容）
# ============================================================

class EventProcessor:
    """事件处理器 — 根据事件类型分发处理

    保留向后兼容接口。内部使用 EventBus 的 subscribe 机制。
    同时兼容旧的 register + process_all 模式。
    """

    def __init__(self, bus: Optional[EventBus] = None):
        self.bus = bus or EventBus()
        self._handlers: Dict[str, Callable[[Event], Any]] = {}
        self._results: Dict[str, Any] = {}  # 存储处理结果
        self._results_lock = threading.Lock()
        self._max_results = 1000  # 防止内存无限增长

    def register(self, event_type: str, handler: Callable[[Event], Any]):
        """注册事件处理器

        同时注册到 EventBus 的 subscribe 系统。
        """
        self._handlers[event_type] = handler
        # 同时注册到 EventBus（兼容新的分发模式）
        self.bus.subscribe(event_type, self._wrap_handler(event_type, handler))
        logger.info(f"[EventProcessor] 注册处理器: {event_type}")

    def _wrap_handler(self, event_type: str, handler: Callable[[Event], Any]) -> Callable[[Event], Any]:
        """包装处理器，用于 EventBus subscribe"""
        def wrapped(event: Event):
            try:
                result = handler(event)
                with self._results_lock:
                    self._results[event.trace_id] = result
                    # 防止内存无限增长：超出上限时淘汰最旧的记录
                    if len(self._results) > self._max_results:
                        for _key in list(self._results.keys())[:len(self._results) - self._max_results]:
                            self._results.pop(_key, None)
                return result
            except Exception:
                logger.warning(f"Unexpected error in mnemos_bus.py", exc_info=True)
                raise  # 让 EventBus 的重试逻辑处理
        wrapped.__name__ = handler.__name__
        return wrapped

    def process_one(self, event: Event) -> Any:
        """处理单个事件（兼容旧接口）"""
        handler = self._handlers.get(event.event_type)
        if not handler:
            logger.warning(f"[EventProcessor] 未找到处理器: {event.event_type}")
            return None

        try:
            result = handler(event)
            self.bus._mark_done(event.trace_id)
            return result
        except Exception as e:
            logger.error(f"[EventProcessor] 处理事件失败 {event.trace_id}: {e}")
            self.bus._mark_failed(event.trace_id, str(e))
            return None

    def process_all(self, event_types: Optional[List[str]] = None, limit: int = 50) -> int:
        """处理所有待处理事件（兼容旧接口）

        .. note::
            推荐使用 bus.start_dispatch() 启动后台分发，
            此方法用于轮询模式的兼容。

        Returns:
            处理的事件数量
        """
        conn = self.bus._get_conn()
        if event_types:
            placeholders = ",".join("?" * len(event_types))
            cursor = conn.execute(
                f"SELECT * FROM events WHERE status = 'pending' AND event_type IN ({placeholders}) ORDER BY id ASC LIMIT ?",
                (*event_types, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM events WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                (limit,),
            )
        rows = cursor.fetchall()

        count = 0
        for row in rows:
            event = Event.from_row(row)
            if event:
                # 标记为 processing
                conn.execute(
                    "UPDATE events SET status = 'processing' WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                self.process_one(event)
                count += 1

        return count


# ============================================================
# 便捷函数
# ============================================================

# 全局 EventBus 单例
_global_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def _get_bus() -> EventBus:
    """获取全局 EventBus 实例"""
    global _global_bus
    if _global_bus is None:
        with _bus_lock:
            if _global_bus is None:
                _global_bus = EventBus()
    return _global_bus


# 公共别名，供外部模块（如 PluggableModule）使用
get_event_bus = _get_bus


def publish_event(event_type: str, agent: str, payload: Dict[str, Any]) -> str:
    """便捷函数：发布事件

    保持旧签名 (event_type, agent, payload) 的兼容性。
    内部转换为新 EventBus.publish 调用。
    """
    bus = _get_bus()
    event = Event(event_type=event_type, source=agent, payload=payload)
    return bus.publish(event)


def get_pending_events(event_types: Optional[List[str]] = None, limit: int = 100) -> List[Event]:
    """便捷函数：获取待处理事件"""
    bus = _get_bus()
    conn = bus._get_conn()
    if event_types:
        placeholders = ",".join("?" * len(event_types))
        cursor = conn.execute(
            f"SELECT * FROM events WHERE status = 'pending' AND event_type IN ({placeholders}) ORDER BY id ASC LIMIT ?",
            (*event_types, limit),
        )
    else:
        cursor = conn.execute(
            "SELECT * FROM events WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
            (limit,),
        )
    return [Event.from_row(row) for row in cursor.fetchall() if Event.from_row(row)]


def get_event_stats() -> Dict[str, int]:
    """便捷函数：获取事件统计"""
    bus = _get_bus()
    return bus.stats()
