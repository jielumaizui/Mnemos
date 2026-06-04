# -*- coding: utf-8 -*-
"""
CaptureWorkerPool — 全局 Worker 池

职责：
- 从 CaptureQueue 取出 pending 事件
- 按 source_agent 隔离并发
- 同一 session 内按 turn_number 顺序处理
- 调用 SyncEngine.sync_single_turn() 写入 Memos
- 单来源失败不影响其他来源

不重复实现：去重、分片、标签组装、信号采集（这些由 SyncEngine 负责）
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.sync_engine import SyncEngine
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class CaptureWorkerPool:
    """全局 Capture Worker 池，按来源隔离"""

    def __init__(
        self,
        queue: Optional[CaptureQueue] = None,
        sync_engine: Optional[SyncEngine] = None,
    ):
        config = get_config()
        self.queue = queue or CaptureQueue()
        self.engine = sync_engine or SyncEngine()
        self.max_workers = config.get("capture.max_workers", 4)
        self.per_source_concurrency = config.get("capture.per_source_concurrency", 1)
        self.max_batch_per_tick = config.get("capture.max_batch_per_tick", 50)
        self.tick_interval = config.get("capture.tick_interval_seconds", 5)

        self._running = False
        self._worker_threads: List[threading.Thread] = []
        self._source_semaphores: Dict[str, threading.Semaphore] = {}
        self._source_semaphore_lock = threading.Lock()
        self._source_errors: Dict[str, int] = defaultdict(int)
        self._source_last_retry: Dict[str, float] = defaultdict(float)

    def start(self):
        """启动 Worker 池"""
        if self._running:
            return
        self._running = True

        # 1. 崩溃恢复：将上次卡住的 processing 回退到 pending
        reset_count = self.queue.reset_processing_to_pending()

        # 2. 加载持久化的退避状态
        self._load_backoff_states()

        for i in range(self.max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"CaptureWorker-{i}",
                daemon=True,
            )
            t.start()
            self._worker_threads.append(t)
        logger.info(
            f"[CaptureWorkerPool] 启动 {self.max_workers} 个 worker, "
            f"每来源并发 {self.per_source_concurrency}, "
            f"恢复 {reset_count} 个卡住事件"
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
        if hasattr(self, 'queue') and self.queue is not None:
            try:
                self.queue.close()
            except Exception:
                pass
        if hasattr(self, 'engine') and self.engine is not None:
            try:
                self.engine.close()
            except Exception:
                pass

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
        for ev in events:
            try:
                self._process_event(ev)
                success += 1
                self._record_success(ev["source_agent"])
            except Exception as e:
                failed += 1
                self._record_error(ev["source_agent"])
                retry_count = ev.get("retry_count", 0)
                if retry_count >= 3:
                    self.queue.update_status(
                        ev["id"], "failed", error=f"flush failed: {e}"
                    )
                else:
                    self.queue.update_status(
                        ev["id"], "pending", error=str(e)
                    )

        logger.info(
            f"[CaptureWorkerPool] flush_session {source_agent}/{session_id}: "
            f"{success} 成功, {failed} 失败"
        )
        return {"flushed": success, "failed": failed, "session_id": session_id}

    def _worker_loop(self):
        """Worker 主循环"""
        while self._running:
            try:
                self._process_one_batch()
            except Exception as e:
                logger.error(f"[CaptureWorker] 批量处理异常: {e}", exc_info=True)

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

    def _process_one_batch(self):
        """处理一批任务"""
        # 1. 优先处理带 session_end 标记的 session
        events = self._dequeue_session_end_markers()

        # 2. 公平 dequeue 补充（round-robin，避免单来源独占 batch）
        if len(events) < self.max_batch_per_tick:
            remaining = self.max_batch_per_tick - len(events)
            regular = self.queue.dequeue_fair(limit=remaining)
            events.extend(regular)

        if not events:
            return

        # 按 source_agent 分组
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for ev in events:
            grouped[ev["source_agent"]].append(ev)

        for source_agent, source_events in grouped.items():
            # 检查该来源是否需要退避
            if self._should_backoff(source_agent):
                # 把事件放回 pending
                for ev in source_events:
                    self.queue.update_status(ev["id"], "pending")
                continue

            # 获取来源并发信号量
            semaphore = self._get_source_semaphore(source_agent)
            if not semaphore.acquire(blocking=False):
                # 并发已满，把事件放回 pending，下一 tick 再试
                for ev in source_events:
                    self.queue.update_status(ev["id"], "pending")
                continue

            try:
                # 按 session_id 再分组，确保同 session 按 turn_number 顺序
                session_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for ev in source_events:
                    session_groups[ev["session_id"]].append(ev)

                for session_id, session_events in session_groups.items():
                    session_events.sort(key=lambda e: e.get("turn_number") or 0)
                    for ev in session_events:
                        try:
                            self._process_event(ev)
                            self._record_success(source_agent)
                        except Exception as e:
                            self._record_error(source_agent)
                            retry_count = ev.get("retry_count", 0)
                            if retry_count >= 3:
                                self.queue.update_status(
                                    ev["id"], "failed", error=f"max retries exceeded: {e}"
                                )
                            else:
                                self.queue.update_status(
                                    ev["id"], "pending", error=str(e)
                                )
            finally:
                semaphore.release()

    def _dequeue_session_end_markers(self) -> List[Dict[str, Any]]:
        """检查 session_end 标记，优先 dequeue 这些 session"""
        events: List[Dict[str, Any]] = []
        try:
            markers = self.queue.get_session_end_markers()
            for marker in markers:
                source_agent = marker["source_agent"]
                session_id = marker["session_id"]
                session_events = self.queue.dequeue_by_session(
                    source_agent, session_id, limit=100
                )
                if session_events:
                    events.extend(session_events)
                # 无论是否有事件，都清除标记（事件已出队或 session 本就没有 pending）
                self.queue.clear_session_end_marker(source_agent, session_id)
        except Exception as e:
            logger.error(f"[CaptureWorker] 处理 session_end 标记失败: {e}")
        return events

    def _should_backoff(self, source_agent: str) -> bool:
        """检查来源是否需要指数退避"""
        error_count = self._source_errors.get(source_agent, 0)
        if error_count == 0:
            return False
        delay = min(5 * (2 ** error_count), 300)
        last_retry = self._source_last_retry.get(source_agent, 0)
        return (time.time() - last_retry) < delay

    def _load_backoff_states(self):
        """从数据库加载持久化的退避状态"""
        try:
            # 尝试读取所有已知 source 的退避状态
            for source in ["codex", "claude", "kimi", "hermes", "openclaw", "aider", "gemini", "cursor", "windsurf"]:
                state = self.queue.get_backoff_state(source)
                if state["error_count"] > 0:
                    self._source_errors[source] = state["error_count"]
                    if state["last_retry_at"]:
                        try:
                            from datetime import datetime
                            dt = datetime.fromisoformat(state["last_retry_at"])
                            self._source_last_retry[source] = dt.timestamp()
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"[CaptureWorkerPool] 加载退避状态失败: {e}")

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
        """记录来源成功（清零退避）"""
        if self._source_errors.get(source_agent, 0) > 0:
            self._source_errors[source_agent] = 0
            self._source_last_retry[source_agent] = 0
            self.queue.clear_backoff_state(source_agent)

    def _process_event(self, event: Dict[str, Any]):
        """处理单个事件"""
        payload = event.get("payload", {})
        source_agent = event["source_agent"]
        session_id = event["session_id"]
        turn_id = event.get("turn_id")
        turn_number = event.get("turn_number", 0)

        # 构建 Turn
        turn = Turn(
            turn_number=turn_number,
            user_content=payload.get("user_content", ""),
            assistant_content=payload.get("assistant_content", ""),
            timestamp=payload.get("timestamp"),
            metadata=payload.get("metadata", {}),
            tool_calls=payload.get("tool_calls") or payload.get("metadata", {}).get("tool_calls", []),
            tool_results=payload.get("tool_results") or payload.get("metadata", {}).get("tool_results", []),
            reasoning=payload.get("reasoning") or payload.get("metadata", {}).get("reasoning", ""),
            attachments=payload.get("attachments") or payload.get("metadata", {}).get("attachments", []),
            raw_event_refs=payload.get("raw_event_refs") or payload.get("metadata", {}).get("raw_event_refs", []),
            source_files=payload.get("source_files") or payload.get("metadata", {}).get("source_files", []),
            completeness=payload.get("completeness") or payload.get("metadata", {}).get("completeness", {}),
        )

        # 构建 Source（从 registry 获取，或用动态 Source）
        from core.sync_framework.registry import AgentRegistry
        source = AgentRegistry.get(source_agent)
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

        self.queue.update_status(event["id"], "done")


class _DynamicAgentSource(AgentSource):
    """为 MCP 上报动态创建的 AgentSource（无文件发现能力）"""

    def __init__(self, name: str, model_tag: str):
        self._name = name
        self._model_tag = model_tag

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_tag(self) -> str:
        return self._model_tag

    def discover_sessions(self):
        return []

    def parse_turns(self, session_path: Path):
        return []
