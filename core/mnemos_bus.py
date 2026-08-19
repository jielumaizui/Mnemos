from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import queue
import sqlite3
import sys
import threading
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Union

from core.config import get_config
from core.cognitive.cognition_episode_event_schema import (
    initialize_cognition_episode_event_schema_in_conn,
    validate_cognition_episode_event_schema,
)
from core.db_utils import _should_force_transient_pool
from core.event_bus_dead_letter_policy import EventBusDeadLetterPolicyMixin
from core.event_bus_state import EventBusStateLifecycleMixin
from core.event_outcome import HandlerOutcome
from core.event_bus_contract import (
    Event,
    _coerce_config_path,
    _resolve_event_db_dir,
    _resolve_events_root,
)
from core.event_bus_lease import (
    EventBusLeaseLifecycleMixin,
    validate_event_bus_lease_schema,
)
from core.event_subscription import (
    subscribe_handler,
    subscription_consumer_id,
    subscription_identity,
    was_handler_processed,
)
from core.ops.event_subject_provenance import (
    ensure_event_subject_provenance_schema,
    event_is_tombstoned,
    record_event_subject_provenance,
)
from core.wiki_event_contract import (
    InvalidWikiMutationEvent,
    canonicalize_wiki_mutation_event,
    predecessor_retry,
    projection_already_complete,
)
from core.wiki_projection_lifecycle import (
    WikiProjectionLedger,
    resolve_wiki_projection_db_path,
)

MAX_QUEUE_DEPTH = 10000
CONN_SECONDS = 30
DEFAULT_MAX_CHAIN_DEPTH = 10
_current_event: contextvars.ContextVar[Optional["Event"]] = contextvars.ContextVar(
    "_current_event", default=None
)
logger = logging.getLogger(__name__)
EVENT_TYPES = [
    "memory_synced",
    "content_scored",
    "knowledge_distilled",
    "cognition_episode_committed",
    "entity_discovered",
    "relation_conflicted",
    "profile_updated",
    "blind_spot_detected",
    "dispute_created",
    "system_alert",
    "wiki_search_requested",
    "distillation_progress",
    "distill_complete",
    "wiki_page_updated",
    "knowledge.ingested",
    "scheduler.daily",
    "polled",
    "session.start",
    "session.end",
    "distill.request",
    "signal.batch",
    "reflection.completed",
    "feedback.prompt_due",
    "observation.updated",
    "knowledge_stale",
    "immune.report",
    "dna.computed",
    "entropy.suggestions",
    "guard_alert",
    "knowledge_needs_reinforcement",
    "profile_blindspot_detected",
    "immune.auto_fix",
]


class EventBus(
    EventBusLeaseLifecycleMixin,
    EventBusDeadLetterPolicyMixin,
    EventBusStateLifecycleMixin,
):
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
            next_attempt_at TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            lease_epoch INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_lease
            ON events(status, lease_expires_at, lease_owner);
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

    _SCHEMA_HANDLER_RECEIPTS = """
        CREATE TABLE IF NOT EXISTS handler_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            handler_name TEXT NOT NULL,
            consumer TEXT NOT NULL,
            disposition TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            mutation_id TEXT NOT NULL DEFAULT '',
            page_revision TEXT NOT NULL DEFAULT '',
            output_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_handler_receipts_trace
            ON handler_receipts(trace_id, handler_name, id);
        CREATE INDEX IF NOT EXISTS idx_handler_receipts_mutation
            ON handler_receipts(mutation_id, consumer, id);
    """

    _SCHEMA_TRACE_CLAIMS = """
        CREATE TABLE IF NOT EXISTS event_trace_claims (
            trace_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_fingerprint TEXT NOT NULL DEFAULT '',
            claimed_at TEXT NOT NULL
        );
    """

    _SCHEMA_DEFERRED_KEYS = """
        CREATE TABLE IF NOT EXISTS event_deferred_keys (
            trace_id TEXT NOT NULL,
            deferred_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(trace_id, deferred_key)
        );
        CREATE INDEX IF NOT EXISTS idx_event_deferred_key
            ON event_deferred_keys(deferred_key);
        CREATE TABLE IF NOT EXISTS event_resolved_deferred_keys (
            deferred_key TEXT PRIMARY KEY,
            resolved_at TEXT NOT NULL
        );
    """

    # 全局单例锁
    _instance_lock = threading.Lock()
    _event_from_row = staticmethod(Event.from_row)

    def __getattribute__(self, name: str):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name == "close" or not callable(attr):
            return attr
        if not callable(getattr(type(self), name, None)):
            return attr

        def release_after_call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                self._release_transient_connections()

        return release_after_call

    def __init__(
        self,
        root_dir: Optional[Path] = None,
        *,
        config: Any | None = None,
        projection_db_path: Path | str | None = None,
        run_startup_maintenance: bool = True,
        recover_pending: bool = True,
        enqueue_published_events: bool = True,
    ):
        # 配置
        runtime_config = config or get_config()
        self._runtime_config = runtime_config
        self._mnemos_dir = _resolve_event_db_dir(runtime_config)
        self._max_retries = runtime_config.get("event_bus.max_retries", 5)
        self._queue_depth_alert = runtime_config.get("event_bus.queue_depth_alert", 1000)
        self._max_queue_depth = runtime_config.get("event_bus.max_queue_depth", MAX_QUEUE_DEPTH)
        self._max_recover_events = runtime_config.get("event_bus.max_recover_events", 1000)
        self._dead_letter_alert = runtime_config.get("event_bus.dead_letter_alert", 10)
        self._dead_letter_max = runtime_config.get("event_bus.dead_letter_max", 1000)
        self._dead_letter_replay_max_age_hours = runtime_config.get(
            "event_bus.dead_letter_replay_max_age_hours", 168
        )
        self._dead_letter_replay_per_type_limit = runtime_config.get(
            "event_bus.dead_letter_replay_per_type_limit", 100
        )
        self._max_latency_ms = runtime_config.get("event_bus.max_latency_ms", 10)
        self._dispatch_workers = max(
            1,
            int(
                runtime_config.get(
                    "event_bus.dispatch_workers",
                    runtime_config.get("event_bus.max_workers", 1),
                )
            ),
        )
        self._handler_timeout_seconds = float(
            runtime_config.get("event_bus.handler_timeout_seconds", 0)
        )
        self._lease_seconds = max(
            30.0,
            float(runtime_config.get("event_bus.lease_seconds", 300.0)),
            self._handler_timeout_seconds * 2 + 30.0,
        )
        self._lease_owner = "eventbus-" + uuid.uuid4().hex
        self._max_chain_depth = max(
            1,
            int(runtime_config.get("event_bus.max_chain_depth", DEFAULT_MAX_CHAIN_DEPTH)),
        )
        self._retry_base_seconds = max(
            0.0, float(runtime_config.get("event_bus.retry_base_seconds", 1.0))
        )
        self._retry_max_seconds = max(
            self._retry_base_seconds,
            float(runtime_config.get("event_bus.retry_max_seconds", 60.0)),
        )
        self._enqueue_published_events = bool(enqueue_published_events)

        self._db_path = self._mnemos_dir / "events.db"
        if projection_db_path is not None:
            explicit_projection_path = Path(projection_db_path).expanduser()
            configured_database_dir = _coerce_config_path(
                getattr(runtime_config, "database_dir", None)
            )
            if configured_database_dir is None:
                raise ValueError("explicit projection_db_path requires config.database_dir")
            canonical_projection_path = configured_database_dir / "wiki_projection.db"
            if explicit_projection_path.resolve(strict=False) != canonical_projection_path.resolve(
                strict=False
            ):
                raise ValueError("explicit projection_db_path must match config.database_dir")
            self._projection_db_path = explicit_projection_path
        else:
            self._projection_db_path = resolve_wiki_projection_db_path(runtime_config)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()  # 每线程独立连接
        self._all_conns: set = set()  # 追踪所有创建的连接，用于 close() 统一清理
        self._conns_lock = threading.Lock()
        self._transient_sqlite = _should_force_transient_pool(self._db_path)
        self._init_db(run_startup_maintenance=run_startup_maintenance)
        self._release_transient_connections()

        self._queue: queue.Queue = queue.Queue()

        self._handlers: Dict[str, List[Callable[[Event], Any]]] = {}
        self._handler_consumer_ids: dict[tuple[str, int], str] = {}
        self._handlers_lock = threading.Lock()

        self._dispatch_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._handler_executor: Optional[ThreadPoolExecutor] = None
        self._in_flight_lock = threading.Lock()
        self._in_flight: set[Future] = set()
        self._active_trace_ids_lock = threading.Lock()
        self._active_trace_ids: set[str] = set()
        self._deferred_lock = threading.Lock()
        self._deferred: deque = deque()

        self.root = (
            _coerce_config_path(root_dir)
            if root_dir is not None
            else _resolve_events_root(runtime_config)
        )
        if self.root is None:
            self.root = _resolve_events_root(runtime_config)
        self._ensure_dirs()

        if recover_pending:
            self._recover_pending()
        self._release_transient_connections()

    @property
    def projection_db_path(self) -> Path:
        """Return the exact Wiki lifecycle ledger bound to this bus."""

        return self._projection_db_path

    def _open_conn(self) -> sqlite3.Connection:
        # Connections remain thread-local while the bus is running. Allow the
        # owner to close worker-created connections after dispatch shutdown.
        conn = sqlite3.connect(str(self._db_path), timeout=CONN_SECONDS, check_same_thread=False)
        conn.row_factory = sqlite3.Row  # noqa
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接"""
        if self._transient_sqlite:
            conn = self._open_conn()
            transient_conns = getattr(self._local, "transient_conns", None)
            if transient_conns is None:
                transient_conns = []
                self._local.transient_conns = transient_conns
            transient_conns.append(conn)
            return conn
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = self._open_conn()
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.add(conn)
        return self._local.conn  # type: ignore[no-any-return]

    def _release_transient_connections(self) -> None:
        """Release short-lived SQLite connections after an operation."""
        if not getattr(self, "_transient_sqlite", False):
            return
        conns = list(getattr(self._local, "transient_conns", []))
        self._local.transient_conns = []
        if hasattr(self._local, "conn"):
            self._local.conn = None
        for conn in conns:
            try:
                conn.close()
            except (sqlite3.Error, OSError):
                logger.warning("EventBus 关闭短连接失败", exc_info=True)

    def _init_db(self, *, run_startup_maintenance: bool = True):
        """Initialize schemas and optionally perform ordinary startup retention."""
        conn = self._get_conn()
        handler_receipts_was_absent = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='handler_receipts'"
            ).fetchone()
            is None
        )
        conn.executescript(self._SCHEMA_EVENTS)
        validate_event_bus_lease_schema(conn)
        conn.executescript(self._SCHEMA_DEAD_LETTERS)
        conn.executescript(self._SCHEMA_HANDLER_RECEIPTS)
        if handler_receipts_was_absent:
            initialize_cognition_episode_event_schema_in_conn(conn)
        validate_cognition_episode_event_schema(conn)
        conn.executescript(self._SCHEMA_TRACE_CLAIMS)
        conn.executescript(self._SCHEMA_DEFERRED_KEYS)
        ensure_event_subject_provenance_schema(conn)
        # Existing event identities remain claimed even after their retained
        # event/dead-letter rows age out. Empty fingerprints are unverified claims.
        conn.execute("""INSERT OR IGNORE INTO event_trace_claims
               (trace_id, event_type, source, payload_fingerprint, claimed_at)
               SELECT trace_id, event_type, source, '', created_at FROM events
               ORDER BY id""")
        conn.execute("""INSERT OR IGNORE INTO event_trace_claims
               (trace_id, event_type, source, payload_fingerprint, claimed_at)
               SELECT trace_id, event_type, source, '', created_at FROM dead_letters
               ORDER BY id""")
        # Explicit bootstrap ensures the current handler receipt columns.
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "processed_handlers" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN processed_handlers TEXT DEFAULT '[]'")
            if "next_attempt_at" not in columns:
                conn.execute(
                    "ALTER TABLE events ADD COLUMN next_attempt_at TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
        except (sqlite3.Error, OSError):
            logger.warning("EventBus 添加 processed_handlers 列失败", exc_info=True)
        conn.commit()
        if not run_startup_maintenance:
            return
        # 启动时清理旧数据，防止表无限增长
        try:
            conn.execute("""DELETE FROM events
                   WHERE status='done'
                     AND event_type!='cognition_episode_committed'
                     AND created_at < datetime('now', '-7 days')""")
            conn.execute("""DELETE FROM dead_letters
                   WHERE event_type!='cognition_episode_committed'
                     AND timestamp < datetime('now', '-30 days')""")
            # 超龄 pending 转为 dead_letter，保留审计痕迹，而非静默删除
            conn.execute("""INSERT INTO dead_letters (timestamp, trace_id, event_type, source,
                    payload_json, status, retry_count, created_at, failure_reason)
                SELECT timestamp, trace_id, event_type, source,
                    payload_json, 'expired', retry_count, created_at,
                    'pending event expired after 3 days, moved to dead_letters on startup'
                FROM events
                WHERE status='pending'
                  AND event_type!='cognition_episode_committed'
                  AND created_at < datetime('now', '-3 days')
                """)
            conn.execute("""DELETE FROM events
                   WHERE status='pending'
                     AND event_type!='cognition_episode_committed'
                     AND created_at < datetime('now', '-3 days')""")
            conn.commit()
        except (sqlite3.Error, OSError):
            logger.warning("EventBus 启动清理失败", exc_info=True)

    def close(self):
        """关闭所有线程的数据库连接并停止分发线程"""
        self.stop_dispatch()
        self._release_owned_leases()
        # 先关闭当前线程的连接，避免在 _all_conns 循环中重复关闭
        if hasattr(self._local, "conn") and self._local.conn is not None:
            local_conn = self._local.conn
            with self._conns_lock:
                self._all_conns.discard(local_conn)
            try:
                local_conn.close()
            except (sqlite3.Error, OSError):
                logger.warning("EventBus 关闭线程本地连接失败", exc_info=True)
            self._local.conn = None
        with self._conns_lock:
            for conn in list(self._all_conns):
                try:
                    conn.close()
                except (sqlite3.Error, OSError):
                    logger.warning("EventBus 关闭连接失败", exc_info=True)
            self._all_conns.clear()

    # ---- 启动恢复 ----

    def _recover_pending(self):
        """Claim pending or expired events without stealing a live process lease."""
        limit = int(self._max_recover_events or 1000)
        events, total = self._claim_pending_batch(limit)
        for event in events:
            self._queue.put(event)
        if events:
            logger.info("[EventBus] 恢复 %s 个未完成事件", len(events))
        if total > limit:
            logger.warning(
                "[EventBus] 未完成事件积压 %d 个，本次仅恢复 %d 个，"
                "运行时分发线程将继续分批恢复",
                total,
                limit,
            )

    def _refill_pending_queue(self, batch: int = 500) -> int:
        """P106: 从 SQLite 分批补充 pending 事件到内存队列，防止启动恢复上限导致 backlog 滞留。"""
        try:
            events, _total = self._claim_pending_batch(batch)
            if not events:
                return 0
            for event in events:
                self._queue.put(event)
            logger.debug("[EventBus] 分批补充 %d 个 pending 事件到队列", len(events))
            return len(events)
        except (sqlite3.Error, OSError):
            logger.warning("[EventBus] 分批补充 pending 事件失败", exc_info=True)
            return 0
        finally:
            self._release_transient_connections()

    # ---- 目录管理（旧文件系统兼容） ----

    def _ensure_dirs(self):
        """确保事件目录结构存在"""
        for sub in ["inbox", "processing", "archive"]:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ========== 发布事件 ==========

    # 必须持久化的事件类型（即使暂时没有消费者，也有审计/追溯价值）
    _PERSISTENT_EVENT_TYPES = {
        "session.start",
        "session.end",
        "distill.request",
        "signal.batch",
        "knowledge.ingested",
        "system_alert",
        "dispute_created",
        "knowledge_distilled",
        "cognition_episode_committed",
        "distill_complete",
        "wiki_page_updated",
        "reflection.completed",
        "observation.updated",
        "knowledge_stale",
        "immune.report",
        "polled",
    }

    # 广播/遥测/进度类事件：无消费者时直接丢弃，不进死信，避免死信表膨胀
    _NO_PERSIST_EVENT_TYPES = {
        "memory_synced",
        "content_scored",
        "entity_discovered",
        "relation_conflicted",
        "profile_updated",
        "blind_spot_detected",
        "distillation_progress",
        "wiki_search_requested",
        "scheduler.daily",
        "dna.computed",
        "entropy.suggestions",
        "guard_alert",
        "knowledge_needs_reinforcement",
        "profile_blindspot_detected",
        "immune.auto_fix",  # 自动修复审计日志，无消费者时不进死信
        "feedback.prompt_due",  # 反馈提示事件，无 UI 消费者时不进死信
    }

    def publish(
        self,
        event: Union[Event, str],
        payload: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> str:
        """发布事件

        支持 ``publish(event_obj)`` 或 ``publish("event_type", payload={...})``。
        需要声明来源或 subject provenance 时，调用方必须构造 typed ``Event``。

        优化：无消费者且非必须持久化的事件直接丢弃，避免 SQLite 堆积。
        测试可传 force=True 强制持久化。

        Returns:
            trace_id
        """
        if isinstance(event, str):
            event = Event(event_type=event, source="", payload=payload or {})

        # 事件链深度守护：防止 handler 里 publish 造成无限级联
        current = _current_event.get()
        if current is not None:
            event.chain_depth = current.chain_depth + 1
        if event.chain_depth >= self._max_chain_depth:
            logger.warning(
                "[EventBus] 事件链深度 %d 超过上限 %d，已丢弃: %s",
                event.chain_depth,
                self._max_chain_depth,
                event.event_type,
            )
            return event.trace_id

        # [P1-7] 无消费者事件处理：
        # - 遥测/广播事件直接丢弃，避免死信表膨胀
        # - 业务事件写入 dead_letter，保留审计痕迹
        event_type = event.event_type
        with self._handlers_lock:
            has_handler = event_type in self._handlers or "*" in self._handlers
        if not force and not has_handler:
            if event_type in self._NO_PERSIST_EVENT_TYPES:
                logger.debug("[EventBus] 遥测事件无消费者，直接丢弃: %s", event_type)
                return event.trace_id
            if event_type not in self._PERSISTENT_EVENT_TYPES:
                self._dead_letter_no_consumer(event)
                return event.trace_id

        # ---- 持久化到 SQLite ----
        trace_id = event.trace_id
        conn = self._get_conn()
        if event_is_tombstoned(conn, trace_id):
            raise PermissionError(f"event trace_id {trace_id!r} is tombstoned")
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        payload_fingerprint = hashlib.sha256(
            "\x1f".join((event.event_type, event.source, payload_json)).encode("utf-8")
        ).hexdigest()
        try:
            claim = conn.execute(
                """INSERT OR IGNORE INTO event_trace_claims
                   (trace_id, event_type, source, payload_fingerprint, claimed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event.trace_id,
                    event.event_type,
                    event.source,
                    payload_fingerprint,
                    now,
                ),
            )
            if claim.rowcount == 0:
                existing = conn.execute(
                    """SELECT event_type, source, payload_fingerprint
                       FROM event_trace_claims WHERE trace_id=?""",
                    (event.trace_id,),
                ).fetchone()
                conn.rollback()
                if (
                    existing
                    and existing["payload_fingerprint"]
                    and (
                        existing["event_type"] != event.event_type
                        or existing["source"] != event.source
                        or existing["payload_fingerprint"] != payload_fingerprint
                    )
                ):
                    raise ValueError(
                        f"trace_id {event.trace_id!r} is already claimed by a different event"
                    )
                return trace_id

            conn.execute(
                """INSERT INTO events
                   (timestamp, trace_id, event_type, source, payload_json,
                    status, retry_count, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (
                    event.timestamp,
                    event.trace_id,
                    event.event_type,
                    event.source,
                    payload_json,
                    now,
                ),
            )
            record_event_subject_provenance(
                conn,
                trace_id=event.trace_id,
                subject_provenance=event.subject_provenance,
                ownership_config=self._runtime_config,
            )
        finally:
            if sys.exc_info()[0] is None:
                conn.commit()
            else:
                conn.rollback()
                logger.warning(
                    "[EventBus] event publish transaction rolled back for trace_id=%s",
                    event.trace_id,
                    exc_info=True,
                )

        if not self._enqueue_published_events:
            return trace_id

        qsize = self._queue.qsize()
        if qsize >= self._max_queue_depth:
            logger.warning(
                "[EventBus] 内存队列深度 %d 已达上限 %d，事件已持久化但暂不入内存队列",
                qsize,
                self._max_queue_depth,
            )
            return trace_id

        # ---- 推入内存队列 ----
        self._queue.put(event)

        # 检查队列深度告警
        qsize = self._queue.qsize()
        if qsize > self._queue_depth_alert:
            logger.warning("[EventBus] 队列深度 %s 超过告警阈值 %s", qsize, self._queue_depth_alert)

        logger.info(
            "[EventBus] 发布事件: %s from %s trace_id=%s", event.event_type, event.source, trace_id
        )
        return trace_id

    # ========== 订阅事件 ==========

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[Event], Any],
        *,
        consumer_id: str | None = None,
    ):
        """注册事件处理器

        Args:
            event_type: 事件类型（支持通配符 "*"）
            handler: 处理函数，接受 Event 参数
        """
        subscribe_handler(self, event_type, handler, consumer_id)
        logger.info("[EventBus] 订阅: %s -> %s", event_type, handler.__name__)

    @staticmethod
    def _handler_display_name(handler: Callable[[Event], Any]) -> str:
        return str(getattr(handler, "__name__", None) or repr(handler))

    # ========== 分发循环 ==========

    def start_dispatch(self):
        """启动后台分发线程与执行器"""
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            return
        self._stop_event.clear()
        with self._in_flight_lock:
            self._in_flight.clear()
        with self._active_trace_ids_lock:
            self._active_trace_ids.clear()
        with self._deferred_lock:
            self._deferred.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self._dispatch_workers,
            thread_name_prefix="EventBus-Worker",
        )
        handler_workers = max(4, self._dispatch_workers * 2)
        self._handler_executor = ThreadPoolExecutor(
            max_workers=handler_workers,
            thread_name_prefix="EventBus-Handler",
        )
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="EventBus-Dispatch"
        )
        self._dispatch_thread.start()
        logger.info("[EventBus] 分发线程已启动 (workers=%s)", self._dispatch_workers)

    def stop_dispatch(self):
        """停止分发线程并等待进行中的事件处理完成"""
        self._stop_event.set()
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=5)
        # 等待已提交但未完成的任务（最多 10 秒）
        if self._executor:
            with self._in_flight_lock:
                futures = set(self._in_flight)
            if futures:
                logger.info("[EventBus] 等待 %s 个进行中的事件完成", len(futures))
                wait(futures, timeout=10)
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._handler_executor:
            self._handler_executor.shutdown(wait=True)
            self._handler_executor = None
        logger.info("[EventBus] 分发线程已停止")

    def _dispatch_loop(self):
        """后台分发循环：从内存队列取事件，提交到线程池执行

        使用 queue.get(timeout=1.0) 阻塞等待，避免忙等空转；
        队列为空时从 SQLite 分批补充 pending 事件（P106）。
        收到 stop_event 后不再提交新任务，等待进行中的任务完成后退出。
        同一 trace_id 的事件不会并发处理，避免状态机竞争。
        """
        while not self._stop_event.is_set():
            # 先尝试处理因 trace_id 冲突被延迟的事件
            if self._flush_deferred_once():
                continue

            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                # P106: 内存队列空时，从持久化存储补充下一批 pending 事件
                self._refill_pending_queue(batch=500)
                continue

            self._submit_event(event)

        # 退出前清空延迟队列，避免残留事件未 task_done
        while self._deferred:
            self._flush_deferred_once()

    def _submit_event(self, event: Event) -> None:
        """将事件提交到执行器；若同一 trace_id 正在处理则延迟。"""
        if not self._claim_event(event):
            self._queue.task_done()
            return
        if not self._executor:
            # 未启动执行器时同步处理，保持显式 start 前的确定性行为。
            try:
                self._dispatch_event(event)
            finally:
                self._queue.task_done()
            return

        trace_id = event.trace_id
        with self._active_trace_ids_lock:
            if trace_id in self._active_trace_ids:
                with self._deferred_lock:
                    self._deferred.append(event)
                return
            self._active_trace_ids.add(trace_id)

        future = self._executor.submit(self._dispatch_event, event)
        with self._in_flight_lock:
            self._in_flight.add(future)
        future.add_done_callback(lambda f: self._on_dispatch_done(f, trace_id))

    def _on_dispatch_done(self, future: Future, trace_id: str) -> None:
        """事件处理完成后的清理：移除跟踪、调用 task_done、尝试 flush 延迟事件。"""
        try:
            future.result()
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            logger.error("[EventBus] 事件处理任务异常", exc_info=True)
        finally:
            self._release_transient_connections()
        with self._in_flight_lock:
            self._in_flight.discard(future)
        with self._active_trace_ids_lock:
            self._active_trace_ids.discard(trace_id)
        self._queue.task_done()

    def _flush_deferred_once(self) -> bool:
        """尝试处理一个被延迟的事件。返回是否处理了事件。"""
        with self._deferred_lock:
            if not self._deferred:
                return False
            event = self._deferred[0]
            trace_id = event.trace_id
            if not self._claim_event(event):
                self._deferred.popleft()
                self._queue.task_done()
                return True
            with self._active_trace_ids_lock:
                if trace_id in self._active_trace_ids:
                    return False
                self._active_trace_ids.add(trace_id)
            self._deferred.popleft()

        if not self._executor:
            try:
                self._dispatch_event(event)
            finally:
                with self._active_trace_ids_lock:
                    self._active_trace_ids.discard(trace_id)
                self._queue.task_done()
            return True

        future = self._executor.submit(self._dispatch_event, event)
        with self._in_flight_lock:
            self._in_flight.add(future)
        future.add_done_callback(lambda f: self._on_dispatch_done(f, trace_id))
        return True

    def _dispatch_event(self, event: Event):
        """分发单个事件到对应处理器"""
        if not self._claim_event(event):
            return
        if event_is_tombstoned(self._get_conn(), event.trace_id):
            logger.info("[EventBus] tombstoned event blocked before dispatch: %s", event.trace_id)
            return
        # 事件链深度守护
        if event.chain_depth >= self._max_chain_depth:
            logger.warning(
                "[EventBus] 事件链深度 %d 超过上限 %d，移入死信: %s",
                event.chain_depth,
                self._max_chain_depth,
                event.event_type,
            )
            self._dead_letter_chain_depth(event)
            self._release_transient_connections()
            return

        if event.event_type == "wiki_page_updated":
            try:
                event = canonicalize_wiki_mutation_event(event, self._projection_db_path)
            except InvalidWikiMutationEvent as exc:
                self._mark_terminal_failure(event.trace_id, str(exc))
                self._release_transient_connections()
                return

        # 查找处理器
        handlers: List[tuple[str, int, Callable[[Event], Any]]] = []
        with self._handlers_lock:
            handlers.extend(
                (event.event_type, index, handler)
                for index, handler in enumerate(self._handlers.get(event.event_type, []))
            )
            handlers.extend(
                ("*", index, handler) for index, handler in enumerate(self._handlers.get("*", []))
            )

        if not handlers:
            logger.debug("[EventBus] 无处理器: %s", event.event_type)
            # [P1-7] 无消费者事件处理：遥测/广播事件直接丢弃，业务事件写入死信
            if event.event_type in self._NO_PERSIST_EVENT_TYPES:
                self._archive_no_consumer_telemetry(event)
            else:
                self._dead_letter_no_consumer(event)
            self._release_transient_connections()
            return

        # 设置当前事件上下文，使 handler 内 publish 能继承 chain_depth
        token = _current_event.set(event)
        try:
            # 加载已成功的处理器记录，避免重试时重复执行
            conn = self._get_conn()
            row = conn.execute(
                "SELECT processed_handlers FROM events WHERE trace_id = ?", (event.trace_id,)
            ).fetchone()
            processed: set[str] = set()
            if row and row["processed_handlers"]:
                try:
                    processed = set(json.loads(row["processed_handlers"]))
                except (json.JSONDecodeError, TypeError):
                    processed = set()
            self._release_transient_connections()

            identities = [
                subscription_identity(self, subscription_type, handler)
                for subscription_type, _index, handler in handlers
            ]
            display_names = [self._handler_display_name(handler) for _, _, handler in handlers]
            display_counts = {name: display_names.count(name) for name in set(display_names)}

            # 调用处理器，只执行未成功过的
            all_ok = True
            failure_reasons = []
            terminal_reasons = []
            deferred_reasons = []
            deferred_keys: set[str] = set()
            newly_succeeded: list[str] = []
            for (subscription_type, index, handler), handler_identity, display_name in zip(
                handlers, identities, display_names
            ):
                checkpointed = display_counts[display_name] == 1 and was_handler_processed(
                    self, processed, subscription_type, handler, display_name
                )
                if handler_identity in processed or checkpointed:
                    continue
                try:
                    consumer_id = subscription_consumer_id(self, subscription_type, handler)
                    if projection_already_complete(event, consumer_id, self._projection_db_path):
                        self._record_handler_receipt(
                            event,
                            handler_identity,
                            HandlerOutcome.noop(consumer_id, "projection receipt already terminal"),
                            record_projection=False,
                        )
                        newly_succeeded.append(handler_identity)
                        continue
                    deferred = predecessor_retry(
                        event,
                        consumer_id,
                        self._projection_db_path,
                    )
                    if deferred is not None:
                        self._record_handler_receipt(
                            event,
                            handler_identity,
                            deferred,
                            record_projection=False,
                        )
                        all_ok = False
                        deferred_reasons.append(f"{deferred.consumer}: {deferred.reason}")
                        deferred_keys.update(
                            str(item)
                            for item in deferred.metadata.get("deferred_keys", [])
                            if str(item)
                        )
                        continue
                    result = self._invoke_handler(handler, event, display_name)
                    outcome = HandlerOutcome.from_result(result, consumer=handler_identity)
                    if outcome.disposition == "defer":
                        self._record_handler_receipt(
                            event, handler_identity, outcome, record_projection=False
                        )
                        deferred_reasons.append(
                            f"{outcome.consumer or display_name}: {outcome.reason}"
                        )
                        deferred_keys.update(
                            str(item)
                            for item in outcome.metadata.get("deferred_keys", [])
                            if str(item)
                        )
                        all_ok = False
                        continue
                    self._record_handler_receipt(event, handler_identity, outcome)
                    if outcome.disposition in {"ack", "noop"}:
                        newly_succeeded.append(handler_identity)
                    elif outcome.disposition == "retry":
                        all_ok = False
                        failure_reasons.append(
                            f"{outcome.consumer or display_name}: {outcome.reason or 'retry requested'}"
                        )
                    else:
                        all_ok = False
                        terminal_reasons.append(
                            f"{outcome.consumer or display_name}: {outcome.reason or 'terminal failure'}"
                        )
                except (
                    ImportError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    sqlite3.Error,
                ) as e:
                    all_ok = False
                    failure_reasons.append(f"{display_name}: {e}")
                    self._record_handler_receipt(
                        event,
                        handler_identity,
                        HandlerOutcome.retry(handler_identity, str(e)),
                    )
                    logger.error(
                        "[EventBus] 处理器 %s 处理事件 %s 失败: %s",
                        display_name,
                        event.event_type,
                        e,
                        exc_info=True,
                    )

            # 持久化本次成功的处理器
            if newly_succeeded:
                processed.update(newly_succeeded)
                conn = self._get_conn()
                updated = conn.execute(
                    """UPDATE events SET processed_handlers = ?
                       WHERE trace_id = ? AND status='processing' AND lease_owner=?""",
                    (
                        json.dumps(list(processed), ensure_ascii=False),
                        event.trace_id,
                        self._lease_owner,
                    ),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    raise RuntimeError("EventBus lease was lost before handler checkpoint")
                conn.commit()

            if terminal_reasons:
                self._mark_terminal_failure(event.trace_id, "; ".join(terminal_reasons))
            elif deferred_reasons:
                self._mark_deferred(event.trace_id, "; ".join(deferred_reasons), deferred_keys)
            elif all_ok:
                self._mark_done(event.trace_id)
            else:
                self._mark_failed(event.trace_id, "; ".join(failure_reasons))
        finally:
            _current_event.reset(token)
            self._release_transient_connections()

    def _record_handler_receipt(
        self,
        event: Event,
        handler_name: str,
        outcome: HandlerOutcome,
        *,
        record_projection: bool = True,
    ) -> None:
        consumer = outcome.consumer or handler_name
        mutation_id = str(event.payload.get("mutation_id") or "")
        page_revision = str(event.payload.get("page_revision") or "")
        output_json = json.dumps(outcome.metadata, ensure_ascii=False, sort_keys=True)
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute(
            """SELECT 1 FROM events
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (event.trace_id, self._lease_owner),
        ).fetchone()
        if lease is None:
            conn.rollback()
            raise RuntimeError("EventBus lease was lost before handler receipt")
        if event.event_type == "cognition_episode_committed" and outcome.disposition in {
            "ack",
            "noop",
        }:
            existing_terminal = conn.execute(
                """SELECT disposition, output_json FROM handler_receipts
                   WHERE trace_id=? AND consumer=?
                     AND disposition IN ('ack','noop')
                   ORDER BY id""",
                (event.trace_id, consumer),
            ).fetchall()
            if len(existing_terminal) > 1:
                conn.rollback()
                raise RuntimeError("cognition episode handler has duplicate terminal receipts")
            if existing_terminal:
                if str(existing_terminal[0]["output_json"]) != output_json:
                    conn.rollback()
                    raise RuntimeError(
                        "cognition episode terminal handler receipt conflicts with replay"
                    )
                conn.commit()
                return
        conn.execute(
            """INSERT INTO handler_receipts
               (trace_id, event_type, handler_name, consumer, disposition, reason,
                mutation_id, page_revision, output_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.trace_id,
                event.event_type,
                handler_name,
                consumer,
                outcome.disposition,
                outcome.reason,
                mutation_id,
                page_revision,
                output_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        if record_projection and event.event_type == "wiki_page_updated" and mutation_id:
            WikiProjectionLedger(self._projection_db_path).record_projection_receipt(
                mutation_id=mutation_id,
                consumer=consumer,
                outcome=outcome.disposition,
                reason=outcome.reason,
                event_trace_id=event.trace_id,
                metadata=outcome.metadata,
            )
            if outcome.disposition in {"ack", "noop"}:
                self.resume_deferred(f"projection:{mutation_id}:{consumer}")

    # ---- SQLite 状态更新 ----

    def _mark_done(self, trace_id: str):
        """标记事件为 done"""
        conn = self._get_conn()
        updated = conn.execute(
            """UPDATE events
               SET status='done', lease_owner='', lease_expires_at=''
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (trace_id, self._lease_owner),
        ).rowcount
        if updated != 1:
            conn.rollback()
            raise RuntimeError("EventBus lease was lost before done transition")
        conn.execute("DELETE FROM event_deferred_keys WHERE trace_id=?", (trace_id,))
        conn.commit()

    def _mark_terminal_failure(self, trace_id: str, reason: str) -> None:
        """Move a non-retryable business failure directly to the durable DLQ."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO dead_letters (
                   timestamp, trace_id, event_type, source, payload_json,
                   status, retry_count, created_at, failure_reason
               )
               SELECT timestamp, trace_id, event_type, source, payload_json,
                   'dead', retry_count, created_at, ?
               FROM events
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (reason, trace_id, self._lease_owner),
        )
        deleted = conn.execute(
            """DELETE FROM events
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (trace_id, self._lease_owner),
        ).rowcount
        if deleted != 1:
            conn.rollback()
            raise RuntimeError("EventBus lease was lost before terminal transition")
        conn.execute("DELETE FROM event_deferred_keys WHERE trace_id=?", (trace_id,))
        conn.commit()

    def _dead_letter_no_consumer(self, event: Event) -> None:
        """[P1-7] 将无消费者事件移入死信队列，保留审计痕迹"""
        conn = self._get_conn()
        # This path persists a body without passing through ``publish``'s
        # pending-event transaction.  Bind its provenance before writing the
        # dead letter so an exact subject deletion never has to infer from
        # payload text or treat a newly written object as historical.
        record_event_subject_provenance(
            conn,
            trace_id=event.trace_id,
            subject_provenance=event.subject_provenance,
            ownership_config=self._runtime_config,
        )
        # 先尝试删除 events 表中同 trace_id 的记录（可能之前是 pending）
        conn.execute("DELETE FROM events WHERE trace_id = ?", (event.trace_id,))
        # 写入 dead_letters
        conn.execute(
            """INSERT INTO dead_letters
               (
                   timestamp, trace_id, event_type, source, payload_json,
                   status, retry_count, created_at, failure_reason
               )
               VALUES (?, ?, ?, ?, ?, 'no_consumer', 0, ?, ?)""",
            (
                event.timestamp,
                event.trace_id,
                event.event_type,
                event.source,
                json.dumps(event.payload),
                datetime.now(timezone.utc).isoformat(),
                "no registered handler for this event type",
            ),
        )
        conn.commit()
        logger.info(
            "[EventBus] 无消费者事件 %s (%s) 移入死信队列", event.trace_id, event.event_type
        )

    def _dead_letter_chain_depth(self, event: Event) -> None:
        """将超出链深度上限的事件移入死信队列，保留审计痕迹。"""
        conn = self._get_conn()
        # Chain-depth rejection can occur before the normal publish write;
        # retain the same typed provenance contract as all other durable
        # EventBus payloads.
        record_event_subject_provenance(
            conn,
            trace_id=event.trace_id,
            subject_provenance=event.subject_provenance,
            ownership_config=self._runtime_config,
        )
        conn.execute("DELETE FROM events WHERE trace_id = ?", (event.trace_id,))
        conn.execute(
            """INSERT INTO dead_letters
               (
                   timestamp, trace_id, event_type, source, payload_json,
                   status, retry_count, created_at, failure_reason
               )
               VALUES (?, ?, ?, ?, ?, 'chain_depth_exceeded', 0, ?, ?)""",
            (
                event.timestamp,
                event.trace_id,
                event.event_type,
                event.source,
                json.dumps(event.payload),
                datetime.now(timezone.utc).isoformat(),
                f"event chain depth {event.chain_depth} >= {self._max_chain_depth}",
            ),
        )
        conn.commit()
        logger.info(
            "[EventBus] 事件 %s (%s) 因链深度超限移入死信队列",
            event.trace_id,
            event.event_type,
        )

    def _archive_no_consumer_telemetry(self, event: Event) -> None:
        conn = self._get_conn()
        updated = conn.execute(
            "UPDATE events SET status = 'archived' WHERE trace_id = ?",
            (event.trace_id,),
        ).rowcount
        conn.commit()
        if updated:
            logger.info(
                "[EventBus] 无消费者遥测事件 %s (%s) 已归档",
                event.trace_id,
                event.event_type,
            )

    # ========== 查询接口 ==========

    def get_dead_letters(self, event_type: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """查询死信队列"""
        conn = self._get_conn()
        if event_type:
            cursor = conn.execute(
                "SELECT * FROM dead_letters WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            )
        else:
            cursor = conn.execute("SELECT * FROM dead_letters ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def replay_dead_letter(self, trace_id: str) -> bool:
        """将死信事件重新放回事件队列"""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM dead_letters WHERE trace_id = ?", (trace_id,)).fetchone()
        if not row:
            return False
        if event_is_tombstoned(conn, trace_id):
            return False

        event = Event.from_row(row)
        if not event:
            return False

        # 重新插入 events 表
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO events (
               timestamp, trace_id, event_type, source,
               payload_json, status, retry_count, created_at
           )
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
        logger.info("[EventBus] 重放死信事件: %s", trace_id)
        return True

    def _replay_one_dead_letter(self, row: sqlite3.Row) -> bool:
        """反序列化、去重、插入 pending、删除死信、放入内存队列。"""
        conn = self._get_conn()
        if event_is_tombstoned(conn, str(row["trace_id"])):
            return False
        event = Event.from_row(row)
        if not event:
            return False
        existing = conn.execute(
            "SELECT 1 FROM events WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM dead_letters WHERE id = ?", (row["id"],))
            return False
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO events
               (timestamp, trace_id, event_type, source, payload_json,
                status, retry_count, created_at)
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
        conn.execute("DELETE FROM dead_letters WHERE id = ?", (row["id"],))
        self._queue.put(event)
        return True

    def replay_no_consumer_dead_letters(
        self,
        event_types: Optional[List[str]] = None,
        limit: int = 100,
        max_age_hours: Optional[int] = None,
        per_type_limit: Optional[int] = None,
    ) -> int:
        """重放当前已有消费者的 no_consumer 死信事件。

        只重放已经注册消费者的事件类型，避免把仍无消费者的事件重新放回
        events 表后再次进入 no_consumer 死信。

        增加了时间窗口和单类型上限，防止启动时一次性涌入大量历史死信。
        """
        with self._handlers_lock:
            has_wildcard = "*" in self._handlers
            handled_types = set(self._handlers.keys()) - {"*"}

        requested_types = set(event_types or [])
        if requested_types:
            handled_types = requested_types if has_wildcard else handled_types & requested_types
        if not has_wildcard and not handled_types:
            return 0

        rows = self._select_no_consumer_candidates(event_types, limit, handled_types, has_wildcard)
        if not rows:
            return 0

        rows = self._filter_replay_rows(rows, max_age_hours, per_type_limit)
        if not rows:
            return 0

        replayed = 0
        for row in rows:
            if self._replay_one_dead_letter(row):
                replayed += 1
        self._get_conn().commit()
        if replayed:
            logger.info("[EventBus] 重放 no_consumer 死信事件: %s", replayed)
        return replayed


_global_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def _get_bus(*, config: Any | None = None) -> EventBus:
    """Return the process bus, fail-closed on conflicting durable targets."""

    global _global_bus
    if _global_bus is None:
        with _bus_lock:
            if _global_bus is None:
                _global_bus = EventBus(config=config)
    if config is not None:
        requested_event_db = (_resolve_event_db_dir(config) / "events.db").resolve(strict=False)
        actual_event_db = _global_bus._db_path.resolve(strict=False)
        if requested_event_db != actual_event_db:
            raise RuntimeError(
                "global EventBus is bound to a different durable event database: "
                f"{actual_event_db} != {requested_event_db}"
            )
        requested_projection_db = resolve_wiki_projection_db_path(config).resolve(strict=False)
        actual_projection_db = _global_bus.projection_db_path.resolve(strict=False)
        if requested_projection_db != actual_projection_db:
            raise RuntimeError(
                "global EventBus is bound to a different Wiki projection database: "
                f"{actual_projection_db} != {requested_projection_db}"
            )
    return _global_bus


# 公共别名，供外部模块（如 PluggableModule）使用
get_event_bus = _get_bus


def reset_event_bus() -> None:
    """关闭并清空全局 EventBus 单例，用于 daemon 重启或测试隔离。"""
    global _global_bus
    bus = _global_bus
    if bus is not None:
        try:
            bus.close()
        except (sqlite3.Error, OSError):
            logger.warning("[EventBus] 关闭全局单例失败", exc_info=True)
        finally:
            _global_bus = None


def publish_event(
    event_type: str,
    agent: str,
    payload: Dict[str, Any],
    *,
    trace_id: str = "",
    subject_provenance: Optional[Dict[str, Any]] = None,
    config: Any | None = None,
) -> str:
    """发布事件，保持旧签名并转换为 EventBus.publish 调用。"""
    if _global_bus is None and event_type in EventBus._NO_PERSIST_EVENT_TYPES:
        event = Event(
            event_type=event_type,
            source=agent,
            payload=payload,
            trace_id=trace_id,
            subject_provenance=subject_provenance,
        )
        logger.debug("[EventBus] 遥测事件无全局消费者，跳过 EventBus 初始化: %s", event_type)
        return event.trace_id
    return _get_bus(config=config).publish(
        Event(
            event_type=event_type,
            source=agent,
            payload=payload,
            trace_id=trace_id,
            subject_provenance=subject_provenance,
        )
    )


def get_event_stats() -> Dict[str, int]:
    """便捷函数：获取事件统计"""
    bus = _get_bus()
    return bus.stats()
