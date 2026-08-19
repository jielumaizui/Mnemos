# -*- coding: utf-8 -*-
"""
CaptureWorkerPool — 全局 Worker 池

职责：
- 从 CaptureQueue 取出 pending 事件
- 按 source_agent 隔离并发
- 同一 session 内按 turn_number 顺序处理
- 调用 SyncEngine 记录同步状态；已有 raw receipt 时复用 canonical revision
- 单来源失败不影响其他来源

不重复实现：去重、分片、标签组装、信号采集（这些由 SyncEngine 负责）
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.resource_budget import get_budget
from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.sync_engine import SyncEngine
from core.sync_framework.agent_source import (
    TURN_STRUCTURED_METADATA_KEYS,
    AgentSource,
    SessionInfo,
    Turn,
    parse_discovered_session,
)

logger = logging.getLogger(__name__)

RECOVERABLE_CAPTURE_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
    AttributeError,
    RuntimeError,
    sqlite3.Error,
)


def _payload_to_turn(payload: Dict[str, Any], turn_number: int) -> Turn:
    """将 CaptureEvent payload 转换为 Turn。"""
    legacy_metadata = dict(payload.get("metadata", {}) or {})
    metadata = {
        key: value
        for key, value in legacy_metadata.items()
        if key not in TURN_STRUCTURED_METADATA_KEYS
    }
    return Turn(
        turn_number=turn_number,
        user_content=payload.get("user_content", ""),
        assistant_content=payload.get("assistant_content", ""),
        timestamp=payload.get("timestamp"),
        metadata=metadata,
        tool_calls=(
            payload.get("tool_calls", [])
            if "tool_calls" in payload
            else legacy_metadata.get("tool_calls", [])
        ),
        tool_results=(
            payload.get("tool_results", [])
            if "tool_results" in payload
            else legacy_metadata.get("tool_results", [])
        ),
        reasoning=(
            payload.get("reasoning", "")
            if "reasoning" in payload
            else legacy_metadata.get("reasoning", "")
        ),
        attachments=(
            payload.get("attachments", [])
            if "attachments" in payload
            else legacy_metadata.get("attachments", [])
        ),
        raw_event_refs=(
            payload.get("raw_event_refs", [])
            if "raw_event_refs" in payload
            else legacy_metadata.get("raw_event_refs", [])
        ),
        source_files=(
            payload.get("source_files", [])
            if "source_files" in payload
            else legacy_metadata.get("source_files", [])
        ),
        completeness=(
            payload.get("completeness", {})
            if "completeness" in payload
            else legacy_metadata.get("completeness", {})
        ),
    )


class CaptureWorkerPool:
    """全局 Capture Worker 池，按来源隔离"""

    def __init__(
        self,
        queue: Optional[CaptureQueue] = None,
        sync_engine: Optional[SyncEngine] = None,
        config: Any | None = None,
    ):
        engine_config = getattr(sync_engine, "config", None)
        engine_database_dir = getattr(engine_config, "database_dir", None)
        if config is not None:
            self.config: Any = config
        elif isinstance(engine_database_dir, (str, Path)):
            self.config = engine_config
        else:
            self.config = get_config()
        self.queue = queue or CaptureQueue()
        self.engine = sync_engine or SyncEngine(config=self.config)
        self.max_workers = self.config.get("capture.max_workers", 4)
        self.per_source_concurrency = self.config.get("capture.per_source_concurrency", 1)
        self.max_batch_per_tick = self.config.get("capture.max_batch_per_tick", 50)
        self.tick_interval = self.config.get("capture.tick_interval_seconds", 5)

        self._running = False
        self._worker_threads: List[threading.Thread] = []
        self._source_semaphores: Dict[str, threading.Semaphore] = {}
        self._source_semaphore_lock = threading.Lock()
        self._source_errors: Dict[str, int] = defaultdict(int)
        self._source_last_retry: Dict[str, float] = defaultdict(float)
        self._budget = get_budget()
        # [S31] 定期校准 CaptureQueue 内存 pending 计数器，避免长期漂移。
        self._recalibrate_interval = 30.0
        self._last_recalibrate = 0.0
        self._recalibrate_lock = threading.Lock()

    def start(self):
        """启动 Worker 池"""
        if self._running:
            return
        self._running = True

        # 1. 崩溃恢复：将上次卡住的 processing 回退到 pending
        reset_count = self.queue.reset_processing_to_pending()

        # 2. 加载持久化的退避状态
        self._load_backoff_states()

        # 3. 先恢复已经完成 L1、但尚未拿到 Amphora receipt 的 outbox。
        try:
            self.dispatch_pending_handoffs()
        except RECOVERABLE_CAPTURE_ERRORS as exc:
            logger.warning("[CaptureWorkerPool] 启动时恢复蒸馏 handoff 失败: %s", exc)

        for i in range(self.max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"CaptureWorker-{i}",
                daemon=True,
            )
            t.start()
            self._worker_threads.append(t)
        logger.info(
            "[CaptureWorkerPool] 启动 %s 个 worker, 每来源并发 %s, 恢复 %s 个卡住事件",
            self.max_workers,
            self.per_source_concurrency,
            reset_count,
        )

    def stop(self):
        """停止 Worker 池"""
        self._running = False
        for t in self._worker_threads:
            t.join(timeout=5)
        self._worker_threads.clear()
        # 清理内存字典，防止长期运行的 daemon 内存泄漏
        with self._source_semaphore_lock:
            self._source_semaphores.clear()
        self._source_errors.clear()
        self._source_last_retry.clear()
        logger.info("[CaptureWorkerPool] 已停止")

    def close(self):
        """关闭所有持久连接"""
        self.stop()
        if hasattr(self, "queue") and self.queue is not None:
            try:
                self.queue.close()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        if hasattr(self, "engine") and self.engine is not None:
            try:
                self.engine.close()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

    def flush_session(self, source_agent: str, session_id: str) -> Dict[str, Any]:
        """
        立即 flush 指定 session 的所有 pending 事件。
        由 end_session 触发，不走定时 tick。
        """
        events = self.queue.dequeue_by_session(source_agent, session_id, limit=100)
        if not events:
            return {"flushed": 0, "session_id": session_id}

        success = 0
        failed = 0
        processed_events: List[Dict[str, Any]] = []
        for ev in events:
            try:
                self._process_event(ev)
                success += 1
                processed_events.append(ev)
                self._record_success(ev["source_agent"])
            except RECOVERABLE_CAPTURE_ERRORS as e:
                failed += 1
                self._record_error(ev["source_agent"])
                retry_count = ev.get("retry_count", 0)
                if retry_count >= 3:
                    self.queue.update_status(ev["id"], "failed", error=f"flush failed: {e}")
                else:
                    self.queue.update_status(ev["id"], "pending", error=str(e))

        handoff = None
        if processed_events:
            self._try_enqueue_distillation(source_agent, session_id, processed_events)
            handoff = self.queue.get_distillation_handoff(source_agent, session_id)

        logger.info(
            "[CaptureWorkerPool] flush_session %s/%s: %s 成功, %s 失败",
            source_agent,
            session_id,
            success,
            failed,
        )
        return {
            "flushed": success,
            "failed": failed,
            "session_id": session_id,
            "status": (
                "partial"
                if failed and success
                else (handoff or {}).get("status", "retryable_failed")
            ),
            "handoff_receipt_id": (handoff or {}).get("receipt_id", ""),
        }

    def _worker_loop(self):
        """Worker 主循环"""
        while self._running:
            # [S31] 每 30 秒全局校准一次 pending 计数器，修正 dequeue/update 带来的漂移。
            # 使用非阻塞锁，避免所有 worker 在校准慢查询上串行等待。
            try:
                if self._recalibrate_lock.acquire(blocking=False):
                    try:
                        if time.time() - self._last_recalibrate > self._recalibrate_interval:
                            self.queue.recalibrate_counters()
                            self._last_recalibrate = time.time()
                    finally:
                        self._recalibrate_lock.release()
            except RECOVERABLE_CAPTURE_ERRORS as e:
                logger.warning("[CaptureWorker] 校准 pending 计数器失败: %s", e)

            # [P1-9] 资源预算检查（P0 不降速，但触发 snapshot 更新）
            try:
                self._budget.throttle_delay("capture_worker")
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

            try:
                self._process_one_batch()
            except RECOVERABLE_CAPTURE_ERRORS as e:
                logger.error("[CaptureWorker] 批量处理异常: %s", e)

            # 分段 sleep 以便快速响应 stop()
            end_time = time.time() + self.tick_interval
            while self._running and time.time() < end_time:
                time.sleep(min(0.5, end_time - time.time()))

    def _get_source_semaphore(self, source_agent: str) -> threading.Semaphore:
        """获取（或创建）指定来源的并发信号量（线程安全）"""
        sem = self._source_semaphores.get(source_agent)
        if sem is not None:
            return sem
        with self._source_semaphore_lock:
            # double-check
            if source_agent not in self._source_semaphores:
                self._source_semaphores[source_agent] = threading.Semaphore(
                    self.per_source_concurrency
                )
            return self._source_semaphores[source_agent]

    def process_batch(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """处理一批任务，供 daemon 定时调用。

        Returns:
            {"processed": int, "errors": int}
        """
        old_limit = self.max_batch_per_tick
        if limit is not None:
            self.max_batch_per_tick = limit
        try:
            handoffs = self.dispatch_pending_handoffs(limit=self.max_batch_per_tick)
            events = self._dequeue_session_end_markers()
            if len(events) < self.max_batch_per_tick:
                events.extend(self.queue.dequeue_fair(limit=self.max_batch_per_tick - len(events)))
            if not events:
                return {"processed": 0, "handoffs": handoffs, "errors": 0}
            self._process_events(events)
            states = self.queue.get_event_statuses([int(event["id"]) for event in events])
            committed = sum(status == "done" for status in states.values())
            errors = len(events) - committed
            status = (
                "committed" if errors == 0 else ("partial" if committed else "retryable_failed")
            )
            return {
                "processed": len(events),
                "committed": committed,
                "handoffs": handoffs,
                "errors": errors,
                "status": status,
            }
        finally:
            self.max_batch_per_tick = old_limit

    def _process_one_batch(self):
        """处理一批任务"""
        self.dispatch_pending_handoffs(limit=self.max_batch_per_tick)
        # 1. 优先处理带 session_end 标记的 session
        events = self._dequeue_session_end_markers()

        # 2. 公平 dequeue 补充（round-robin，避免单来源独占 batch）
        if len(events) < self.max_batch_per_tick:
            remaining = self.max_batch_per_tick - len(events)
            regular = self.queue.dequeue_fair(limit=remaining)
            events.extend(regular)

        if not events:
            return

        self._process_events(events)

    def _process_events(self, events: List[Dict[str, Any]]) -> None:
        """处理已经出队的一批事件。

        调用方负责 dequeue；本方法只消费传入事件，避免同一 tick 内二次出队导致
        process_batch() 报告已处理但实际处理另一批事件。
        """
        # 按 source_agent 分组
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for ev in events:
            grouped[ev["source_agent"]].append(ev)

        for source_agent, source_events in grouped.items():
            # 检查该来源是否需要退避
            if self._should_backoff(source_agent):
                # [P1-009] 改为 deferred 状态并设置到期时间，避免立即重试
                for ev in source_events:
                    self.queue.update_status(
                        ev["id"],
                        "deferred",
                        deferred_until=self._compute_deferred_until(
                            ev.get("retry_count", 0), source_agent
                        ),
                    )
                continue

            # 获取来源并发信号量
            semaphore = self._get_source_semaphore(source_agent)
            if not semaphore.acquire(blocking=False):
                # 并发已满，deferred 一小段时间后再试
                for ev in source_events:
                    self.queue.update_status(
                        ev["id"],
                        "deferred",
                        deferred_until=self._compute_deferred_until(
                            ev.get("retry_count", 0), source_agent
                        ),
                    )
                continue

            try:
                # 按 session_id 再分组，确保同 session 按 turn_number 顺序
                session_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for ev in source_events:
                    session_groups[ev["session_id"]].append(ev)

                for session_id, session_events in session_groups.items():
                    session_events.sort(key=lambda e: e.get("turn_number") or 0)
                    self._process_session_events(source_agent, session_id, session_events)
            finally:
                semaphore.release()

    def _process_registry_session(
        self,
        source_agent: str,
        session_id: str,
        session_events: List[Dict[str, Any]],
        registry_source: AgentSource,
    ) -> None:
        """已注册来源：逐条处理事件并统一入队蒸馏。"""
        processed = []
        for ev in session_events:
            try:
                self._process_event(ev, source=registry_source)
                self._record_success(source_agent)
                processed.append(ev)
            except RECOVERABLE_CAPTURE_ERRORS as e:
                self._handle_event_error(ev, source_agent, e)
        if processed:
            self._try_enqueue_distillation(source_agent, session_id, processed)

    def _build_dynamic_source(
        self,
        source_agent: str,
        session_id: str,
        session_events: List[Dict[str, Any]],
    ) -> "_DynamicAgentSource":
        """为动态来源缓存 payload 并构造 _DynamicAgentSource。"""
        first_payload = session_events[0].get("payload", {}) or {}
        model_tag = first_payload.get("model", source_agent)
        source = _DynamicAgentSource(source_agent, model_tag)
        source.set_session_id(session_id)
        for ev in session_events:
            payload = dict(ev.get("payload", {}) or {})
            payload["turn_number"] = int(ev.get("turn_number") or 0)
            source.cache_payload(session_id, payload)
        return source

    def _process_event_fallback(
        self,
        source_agent: str,
        session_events: List[Dict[str, Any]],
        source: "_DynamicAgentSource",
    ) -> List[Dict[str, Any]]:
        """批量 sync 结果异常时回退到逐条处理。"""
        processed: List[Dict[str, Any]] = []
        for ev in session_events:
            try:
                self._process_event(ev, source=source)
                self._record_success(source_agent)
                processed.append(ev)
            except RECOVERABLE_CAPTURE_ERRORS as e:
                self._handle_event_error(ev, source_agent, e)
        return processed

    def _process_dynamic_batch(
        self,
        source_agent: str,
        session_id: str,
        session_events: List[Dict[str, Any]],
        source: "_DynamicAgentSource",
        session_info: SessionInfo,
    ) -> List[Dict[str, Any]]:
        """动态来源批量 sync；结果数量不符或失败时回退逐条处理。"""
        turns = parse_discovered_session(source, session_info)
        if not turns:
            return self._process_event_fallback(source_agent, session_events, source)

        results = self.engine.sync_turns(
            source=source,
            session_info=session_info,
            turns=turns,
            incremental=True,
            enqueue_distillation=False,
        )
        if len(results) != len(session_events):
            return self._process_event_fallback(source_agent, session_events, source)

        processed: List[Dict[str, Any]] = []
        for ev, turn, result in zip(session_events, turns, results):
            if result.action == "failed":
                self._handle_event_error(
                    ev, source_agent, RuntimeError(result.error or "sync failed")
                )
            else:
                processed.append(ev)
                sync_event_id = str((turn.metadata or {}).get("cognitive_sync_event_id") or "")
                if sync_event_id:
                    payload_metadata = (ev.setdefault("payload", {})).setdefault("metadata", {})
                    payload_metadata["cognitive_sync_event_id"] = sync_event_id
                cognitive_queue_event_id = str(
                    ((ev.get("payload") or {}).get("metadata") or {}).get(
                        "cognitive_queue_event_id"
                    )
                    or ""
                )
                if cognitive_queue_event_id:
                    from core.ops.runtime_flow_telemetry import (
                        record_cognitive_data_consumed,
                    )

                    record_cognitive_data_consumed(
                        cognitive_queue_event_id,
                        consumer_id="capture_worker",
                        outcome="sync_turn_committed",
                        config_or_path=self.config,
                    )
        if processed:
            self._record_success(source_agent)
        return processed

    def _try_enqueue_distillation(
        self,
        source_agent: str,
        session_id: str,
        session_events: List[Dict[str, Any]],
    ) -> bool:
        """Persist an outbox, then attach the durable Amphora receipt."""
        try:
            distill_requested = any(
                (event.get("payload", {}).get("metadata", {}) or {}).get("distill_requested", True)
                is not False
                for event in session_events
            )
            handoff = self.queue.create_distillation_handoff(
                source_agent,
                session_id,
                session_events,
                enabled=bool(self.config.get("distill.auto", True)) and distill_requested,
            )
            if handoff["status"] == "intentional_skip":
                self._complete_session_end(source_agent, session_id)
                return True
            committed = self._dispatch_handoff(handoff)
            if committed:
                self._complete_session_end(source_agent, session_id)
            return committed
        except RECOVERABLE_CAPTURE_ERRORS as e:
            for event in session_events:
                current = (
                    self.queue.get_status(
                        event["source_agent"], event["session_id"], event.get("turn_number")
                    )
                    or {}
                )
                if current.get("status") == "processing":
                    self.queue.update_status(
                        event["id"],
                        "deferred",
                        error=str(e),
                        deferred_until=self._compute_deferred_until(
                            int(event.get("retry_count") or 0) + 1, source_agent
                        ),
                    )
            logger.error(
                "[CaptureWorkerPool] 蒸馏 handoff 失败 %s/%s: %s",
                source_agent,
                session_id,
                e,
                exc_info=True,
            )
            return False

    def _process_session_events(
        self,
        source_agent: str,
        session_id: str,
        session_events: List[Dict[str, Any]],
    ):
        """处理一个 session 的事件，registry 来源按条处理，动态来源批量 sync。"""
        from core.sync_framework.registry import SourceRegistry

        registry_source = SourceRegistry.get(source_agent)
        if registry_source is not None:
            self._process_registry_session(
                source_agent, session_id, session_events, registry_source
            )
            return

        # [P1-008] 动态 AgentSource：缓存 payload，整 session 批量 sync
        source = self._build_dynamic_source(source_agent, session_id, session_events)
        first_payload = session_events[0].get("payload", {}) or {}
        cwd = first_payload.get("cwd") or "."
        session_info = SessionInfo(
            session_id=session_id,
            source_path=Path(cwd),
            working_dir=cwd,
        )
        try:
            processed = self._process_dynamic_batch(
                source_agent, session_id, session_events, source, session_info
            )
        finally:
            source.clear_session(session_id)

        if processed:
            self._try_enqueue_distillation(source_agent, session_id, processed)

    def _handle_event_error(self, ev: Dict[str, Any], source_agent: str, error: Exception):
        """统一处理单事件失败：超过重试次数标记 failed，否则 deferred。"""
        self._record_error(source_agent)
        retry_count = ev.get("retry_count", 0)
        if retry_count >= 3:
            self.queue.update_status(ev["id"], "failed", error=f"max retries exceeded: {error}")
        else:
            self.queue.update_status(
                ev["id"],
                "deferred",
                error=str(error),
                deferred_until=self._compute_deferred_until(retry_count + 1, source_agent),
            )

    def _compute_deferred_until(self, retry_count: int, source_agent: Optional[str] = None) -> str:
        """计算事件下次可重试的时间戳。"""
        delay = min(5 * (2**retry_count), 300)
        if source_agent:
            error_count = self._source_errors.get(source_agent, 0)
            if error_count:
                source_delay = min(5 * (2**error_count), 300)
                delay = max(delay, source_delay)
        return (datetime.now() + timedelta(seconds=delay)).isoformat()

    def _dequeue_session_end_markers(self) -> List[Dict[str, Any]]:
        """检查 session_end 标记，优先 dequeue 这些 session"""
        events: List[Dict[str, Any]] = []
        try:
            markers = self.queue.get_session_end_markers()
            for marker in markers:
                source_agent = marker["source_agent"]
                session_id = marker["session_id"]
                session_events = self.queue.dequeue_by_session(source_agent, session_id, limit=100)
                if session_events:
                    events.extend(session_events)
                if not session_events:
                    if not self.queue.session_has_open_capture(source_agent, session_id):
                        failed = self.queue.session_failed_count(source_agent, session_id)
                        if failed:
                            self.queue.fail_session_end(
                                source_agent,
                                session_id,
                                f"{failed} capture event(s) failed before flush completion",
                            )
                        else:
                            self.queue.clear_session_end_marker(source_agent, session_id)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
            logger.error(
                "[CaptureWorker] 处理 session_end 标记失败: "
                "capture_session_end_dequeue_failed"
            )
            raise
        return events

    def _should_backoff(self, source_agent: str) -> bool:
        """检查来源是否需要指数退避"""
        error_count = self._source_errors.get(source_agent, 0)
        if error_count == 0:
            return False
        delay = min(5 * (2**error_count), 300)
        last_retry = self._source_last_retry.get(source_agent, 0)
        return (time.time() - last_retry) < delay  # type: ignore[no-any-return]

    def _load_backoff_states(self):
        """从数据库加载持久化的退避状态（直接查库，兼容已卸载/未注册 Agent）"""
        try:
            conn = self.queue._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source_agent, error_count, last_retry_at FROM source_backoff WHERE error_count > 0"  # noqa: E501
            )
            for row in cursor.fetchall():
                source, error_count, last_retry_at = row
                self._source_errors[source] = error_count
                if last_retry_at:
                    try:
                        from datetime import datetime

                        dt = datetime.fromisoformat(last_retry_at)
                        self._source_last_retry[source] = dt.timestamp()
                    except (ImportError, ValueError):
                        logger.debug(
                            "[capture_worker] (ImportError, ValueError) suppressed", exc_info=True
                        )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            logger.warning("[CaptureWorkerPool] 加载退避状态失败: %s", e)

    def _record_error(self, source_agent: str):
        """记录来源错误（内存 + 持久化）"""
        self._source_errors[source_agent] += 1
        self._source_last_retry[source_agent] = time.time()
        self.queue.set_backoff_state(
            source_agent,
            self._source_errors[source_agent],
            datetime.now().isoformat(),
        )

    def _record_success(self, source_agent: str):
        """记录来源成功（清零退避；无条件清除数据库，兼容未注册 Agent 的场景）"""
        self._source_errors[source_agent] = 0
        self._source_last_retry[source_agent] = 0
        self.queue.clear_backoff_state(source_agent)

    def _process_event(self, event: Dict[str, Any], source: Optional[AgentSource] = None):
        """处理单个事件"""
        payload = event.get("payload", {}) or {}
        source_agent = event["source_agent"]
        session_id = event["session_id"]
        turn_number = event.get("turn_number", 0)

        # 构建 Turn
        turn = _payload_to_turn(payload, turn_number)

        # 构建 Source（从 registry 获取，或用动态 Source）
        if source is None:
            from core.sync_framework.registry import SourceRegistry

            source = SourceRegistry.get(source_agent)
            if source is None:
                # 动态创建最小 Source
                source = _DynamicAgentSource(source_agent, payload.get("model", source_agent))

        # 构建 SessionInfo
        cwd = payload.get("cwd") or "."
        session_info = SessionInfo(
            session_id=session_id,
            source_path=Path(cwd),
            working_dir=cwd,
        )

        # 调用 SyncEngine（复用完整流水线）
        result = self.engine.sync_single_turn(
            source=source,
            session_info=session_info,
            turn=turn,
            incremental=True,
        )

        if result.action == "failed":
            raise RuntimeError(result.error or "sync failed")

        cognitive_queue_event_id = str(
            (payload.get("metadata") or {}).get("cognitive_queue_event_id") or ""
        )
        if cognitive_queue_event_id:
            from core.ops.runtime_flow_telemetry import record_cognitive_data_consumed

            record_cognitive_data_consumed(
                cognitive_queue_event_id,
                consumer_id="capture_worker",
                outcome="sync_turn_committed",
                config_or_path=self.config,
            )

        # Completion is intentionally deferred until the durable Amphora receipt is attached.

    def _dispatch_handoff(self, handoff: Dict[str, Any]) -> bool:
        """Deliver one durable outbox row; exact retries converge on one Amphora task."""
        from core.kia.amphora import enqueue_with_receipt

        try:
            receipt = enqueue_with_receipt(
                session_id=handoff["session_id"],
                messages=handoff["messages"],
                meta=handoff["meta"],
            )
            if receipt.input_revision != handoff["input_revision"]:
                raise RuntimeError("Amphora acknowledged a different input revision")
            self._record_handoff_provenance(handoff.get("meta") or {}, receipt.task_id)
            self.queue.commit_distillation_handoff(
                handoff["receipt_id"],
                downstream_receipt_id=receipt.receipt_id,
                downstream_task_id=receipt.task_id,
            )
            from core.ops.cognitive_pipeline_receipts import record_capture_worker_handoff

            record_capture_worker_handoff(
                self.config, handoff["session_id"], receipt
            )
            from core.ops.cognitive_pipeline_receipts import record_distillation_handoff

            record_distillation_handoff(
                self.config,
                task={
                    "task_id": receipt.task_id,
                    "session_id": handoff["session_id"],
                    "input_revision": receipt.input_revision,
                    "meta": handoff.get("meta") or {},
                },
            )
            logger.info(
                "[CaptureWorkerPool] 蒸馏 handoff committed %s/%s revision=%s task=%s",
                handoff["source_agent"],
                handoff["session_id"],
                handoff["input_revision"][:12],
                receipt.task_id,
            )
            return True
        except RECOVERABLE_CAPTURE_ERRORS as exc:
            self.queue.fail_distillation_handoff(handoff["receipt_id"], str(exc))
            logger.error(
                "[CaptureWorkerPool] Amphora receipt 获取失败 %s/%s: %s",
                handoff["source_agent"],
                handoff["session_id"],
                exc,
                exc_info=True,
            )
            return False

    @staticmethod
    def _record_handoff_provenance(meta: Dict[str, Any], task_id: str) -> None:
        from core.sync_framework.raw_event_store import RawEventStore

        store = RawEventStore()
        try:
            for ref in (meta.get("raw_event_refs") or []):
                if not isinstance(ref, dict) or not ref.get("revision_id"):
                    continue
                try:
                    span_start = int(ref.get("span_start") or 0)
                    span_end = int(ref.get("span_end") or 0)
                except (TypeError, ValueError):
                    logger.warning(
                        "[CaptureWorkerPool] 跳过畸形 span 的 provenance ref "
                        "revision=%s task=%s",
                        str(ref["revision_id"])[:16],
                        task_id,
                    )
                    continue
                if span_start < 0 or span_end <= span_start:
                    logger.warning(
                        "[CaptureWorkerPool] 跳过无 span 的 provenance ref "
                        "revision=%s task=%s",
                        str(ref["revision_id"])[:16],
                        task_id,
                    )
                    continue
                try:
                    store.record_provenance_edge(
                        source_revision_id=str(ref["revision_id"]),
                        span_start=span_start,
                        span_end=span_end,
                        consumer_type="amphora_task",
                        consumer_id=task_id,
                    )
                except KeyError:
                    logger.warning(
                        "[CaptureWorkerPool] 跳过 raw 中已不存在的 provenance ref "
                        "revision=%s task=%s",
                        str(ref["revision_id"])[:16],
                        task_id,
                    )
        finally:
            store.close()

    def dispatch_pending_handoffs(self, limit: int = 100) -> int:
        """Replay persisted outboxes after failure or process restart."""
        committed = 0
        for handoff in self.queue.list_distillation_handoffs(limit=limit):
            if self._dispatch_handoff(handoff):
                committed += 1
                self._complete_session_end(handoff["source_agent"], handoff["session_id"])
        return committed

    def _complete_session_end(self, source_agent: str, session_id: str) -> None:
        receipt = self.queue.get_session_end_receipt(source_agent, session_id)
        if receipt and receipt.get("status") in {"handoff_pending", "retryable_failed"}:
            if not self.queue.session_has_open_capture(source_agent, session_id):
                failed = self.queue.session_failed_count(source_agent, session_id)
                if failed:
                    self.queue.fail_session_end(
                        source_agent,
                        session_id,
                        f"{failed} capture event(s) failed before flush completion",
                    )
                else:
                    self.queue.clear_session_end_marker(source_agent, session_id)


class _DynamicAgentSource(AgentSource):
    """为 MCP 上报动态创建的 AgentSource（无文件发现能力）。

    [P1-008] 通过进程内 payload 缓存支持 discover_sessions / parse_turns，
    使动态来源也能走 sync_turns 批量同步。
    """

    _cache_lock = threading.Lock()

    def __init__(self, name: str, model_tag: str):
        self._name = name
        self._model_tag = model_tag
        self._session_id: Optional[str] = None
        self._payload_cache: Dict[str, List[Dict[str, Any]]] = {}

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    def cache_payload(self, session_id: str, payload: Dict[str, Any]) -> None:
        with self._cache_lock:
            self._payload_cache.setdefault(session_id, []).append(payload)

    def clear_session(self, session_id: str) -> None:
        with self._cache_lock:
            self._payload_cache.pop(session_id, None)

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_tag(self) -> str:
        return self._model_tag

    def discover_sessions(self) -> List[SessionInfo]:
        with self._cache_lock:
            sessions: List[SessionInfo] = []
            for session_id, payloads in list(self._payload_cache.items()):
                cwd = payloads[0].get("cwd", ".") if payloads else "."
                sessions.append(
                    SessionInfo(
                        session_id=session_id,
                        source_path=Path(cwd),
                        working_dir=cwd,
                    )
                )
            return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        session_id = self._session_id
        if not session_id:
            return []
        with self._cache_lock:
            payloads = list(self._payload_cache.get(session_id, []))
        turns = [_payload_to_turn(payload, payload.get("turn_number", 0)) for payload in payloads]
        turns.sort(key=lambda t: t.turn_number)
        return turns
