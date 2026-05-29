# -*- coding: utf-8 -*-
"""
CaptureService — 统一入口层

职责：
- 接收 MCP / AgentSource / 文件导入的请求
- 参数校验
- 计算 dedupe_key + content_hash
- 查重（capture_events + sync_log 双重校验）
- 入队到 CaptureQueue
- 启动/管理 CaptureWorkerPool

硬约束：
- 不做任何 Memos 写入
- 返回 < 200ms
- 队列满返回 backpressure
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.capture_worker import CaptureWorkerPool
from core.sync_framework.sync_engine import compute_content_hash
from core.sync_framework.agent_source import Turn

logger = logging.getLogger(__name__)


class CaptureService:
    """统一捕获服务入口"""

    _instance: Optional["CaptureService"] = None
    _lock = __import__("threading").Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        queue: Optional[CaptureQueue] = None,
        worker_pool: Optional[CaptureWorkerPool] = None,
        start_worker: bool = True,
    ):
        with self._lock:
            if self._initialized:
                # 如果已经初始化过，但之前没启动 worker，现在需要启动
                if start_worker and hasattr(self, 'worker_pool') and not self.worker_pool._running:
                    self.worker_pool.start()
                return
            self._initialized = True

            self.config = get_config()
            self.queue = queue or CaptureQueue()
            self.worker_pool = worker_pool or CaptureWorkerPool(queue=self.queue)
            self.max_payload_bytes = self.config.get("capture.max_payload_bytes", 200000)
            self.duplicate_ttl_days = self.config.get("capture.duplicate_ttl_days", 30)

            # 启动 worker 池（consumer 进程才启动；MCP producer 传 start_worker=False）
            if start_worker:
                self.worker_pool.start()

    def capture_turn(
        self,
        source_agent: str,
        session_id: str,
        turn_id: Optional[str] = None,
        turn_number: int = 0,
        user_content: str = "",
        assistant_content: str = "",
        timestamp: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        单轮对话上报入口。

        Returns:
            {"status": "queued" | "duplicate" | "backpressure" | "error",
             "duplicate": bool}
        """
        start_time = time.time()

        # 参数校验
        if not source_agent or not session_id:
            return {"status": "error", "message": "source_agent and session_id required"}

        user_content = user_content or ""
        assistant_content = assistant_content or ""

        # 大小限制
        total_bytes = len(user_content.encode("utf-8")) + len(assistant_content.encode("utf-8"))
        if total_bytes > self.max_payload_bytes:
            return {
                "status": "error",
                "message": f"payload too large: {total_bytes} bytes (max {self.max_payload_bytes})",
            }

        # 计算 content_hash（统一使用 SyncEngine 的算法，确保 sync_log 去重兜底有效）
        model_tag = model or source_agent
        content_hash = compute_content_hash(
            user_content=user_content,
            assistant_content=assistant_content,
            turn_number=turn_number,
            model_tag=model_tag,
        )

        # 计算 dedupe_key
        dedupe_key = hashlib.sha256(
            f"{source_agent}:{session_id}:{turn_id or turn_number}:{content_hash}".encode("utf-8")
        ).hexdigest()

        # 查重 1: capture_events
        if self.queue.is_duplicate(dedupe_key, ttl_days=self.duplicate_ttl_days):
            return {"status": "duplicate", "duplicate": True}

        # 查重 2: sync_log 兜底（防止 capture_queue 被清理后重复）
        if self._check_sync_log_duplicate(source_agent, session_id, turn_number, content_hash):
            return {"status": "duplicate", "duplicate": True}

        # 构建 payload
        payload = {
            "user_content": user_content,
            "assistant_content": assistant_content,
            "timestamp": timestamp or datetime.now().isoformat(),
            "model": model or source_agent,
            "cwd": cwd,
            "metadata": metadata or {},
        }

        # 入队
        status = self.queue.enqueue(
            dedupe_key=dedupe_key,
            source_agent=source_agent,
            session_id=session_id,
            turn_id=turn_id,
            turn_number=turn_number,
            payload=payload,
            content_hash=content_hash,
        )

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            f"[CaptureService] capture_turn {source_agent}/{session_id}/turn{turn_number} "
            f"-> {status} ({elapsed_ms:.1f}ms)"
        )

        if status == "backpressure":
            return {"status": "backpressure", "duplicate": False}
        if status == "duplicate":
            return {"status": "duplicate", "duplicate": True}
        if status == "queued":
            return {"status": "queued", "duplicate": False}
        return {"status": "error", "message": "enqueue failed"}

    def capture_session(
        self,
        source_agent: str,
        session_id: str,
        turns: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        批量上报整个 session 的所有轮次。
        """
        results = []
        for turn in turns:
            result = self.capture_turn(
                source_agent=source_agent,
                session_id=session_id,
                turn_id=turn.get("turn_id"),
                turn_number=turn.get("turn_number", 0),
                user_content=turn.get("user_content", ""),
                assistant_content=turn.get("assistant_content", ""),
                timestamp=turn.get("timestamp"),
                model=turn.get("model"),
                cwd=turn.get("cwd"),
                metadata=turn.get("metadata"),
            )
            results.append(result)

        queued = sum(1 for r in results if r["status"] == "queued")
        duplicate = sum(1 for r in results if r["status"] == "duplicate")
        backpressure = sum(1 for r in results if r["status"] == "backpressure")
        error = sum(1 for r in results if r["status"] == "error")

        # 状态优先级: backpressure > queued > error > duplicate
        if backpressure > 0:
            status = "backpressure"
        elif queued > 0:
            status = "queued"
        elif error > 0:
            status = "error"
        else:
            status = "duplicate"

        return {
            "status": status,
            "queued_count": queued,
            "duplicate_count": duplicate,
            "backpressure_count": backpressure,
            "error_count": error,
            "session_id": session_id,
        }

    def end_session(
        self,
        source_agent: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """
        标记 session 结束，异步触发 flush。
        只写标记，不阻塞等待 Memos 写入，确保 < 200ms 返回。
        """
        logger.info(f"[CaptureService] end_session {source_agent}/{session_id}")
        self.queue.mark_session_end(source_agent, session_id)
        return {
            "status": "ok",
            "session_id": session_id,
            "message": "session end recorded, async flush queued",
        }

    def get_status(
        self,
        source_agent: str,
        session_id: str,
        turn_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        查询指定 session/turn 的队列状态。
        """
        record = self.queue.get_status(source_agent, session_id, turn_number)
        if not record:
            return {
                "status": "not_found",
                "source_agent": source_agent,
                "session_id": session_id,
                "turn_number": turn_number,
            }
        return {
            "status": record.get("status"),
            "source_agent": source_agent,
            "session_id": session_id,
            "turn_number": record.get("turn_number"),
            "retry_count": record.get("retry_count", 0),
            "created_at": record.get("created_at"),
            "processed_at": record.get("processed_at"),
            "error": record.get("error"),
        }

    def get_pending_counts(self) -> Dict[str, int]:
        """获取各来源 pending 数量"""
        return {"total": self.queue.get_pending_count()}

    def _check_sync_log_duplicate(
        self,
        source_agent: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
    ) -> bool:
        """查 sync_log 兜底（防止 capture_queue 被清理后重复）"""
        try:
            import sqlite3
            db_path = self.config.data_dir / "sync_log.db"
            if not db_path.exists():
                return False
            with sqlite3.connect(str(db_path), timeout=5) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT content_hash FROM sync_log
                    WHERE agent_name = ? AND session_id = ? AND turn_number = ?
                    LIMIT 1
                    """,
                    (source_agent, session_id, turn_number),
                )
                row = cursor.fetchone()
                if row and row[0] == content_hash:
                    return True
        except Exception as e:
            logger.debug(f"[CaptureService] sync_log 查重失败: {e}")
        return False
