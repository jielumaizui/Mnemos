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
from pathlib import Path
from typing import Dict, List, Optional, Any, TYPE_CHECKING

from core.config import get_config
from core.db_utils import SqlitePool
from core.sync_framework.sync_engine import compute_content_hash
from core.sync_framework.agent_source import Turn

if TYPE_CHECKING:
    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_worker import CaptureWorkerPool

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

            # 延迟导入避免循环导入
            from core.sync_framework.capture_queue import CaptureQueue
            from core.sync_framework.capture_worker import CaptureWorkerPool

            self.config = get_config()
            self.queue = queue or CaptureQueue()
            self.worker_pool = worker_pool or CaptureWorkerPool(queue=self.queue)
            self.max_payload_bytes = self.config.get("capture.max_payload_bytes", 200000)
            self.duplicate_ttl_days = self.config.get("capture.duplicate_ttl_days", 30)
            self._sync_pool = SqlitePool(self.config.data_dir / "sync_log.db")

            # 启动 worker 池（consumer 进程才启动；MCP producer 传 start_worker=False）
            if start_worker:
                self.worker_pool.start()
            # 启动时清理旧 capture_events，防止表无限增长
            try:
                self.queue.cleanup_old(days=30)
            except Exception:
                pass

    def _truncate_with_marker(self, text: str, max_bytes: int) -> str:
        """截断文本到指定字节长度，并添加省略标记"""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        # 截断到完整字符边界
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        # 回退到最近一个完整句子或换行
        for delim in ("\n\n", "\n", "。", "；", "; ", ". "):
            idx = truncated.rfind(delim)
            if idx > max_bytes * 0.5:
                truncated = truncated[:idx]
                break
        return truncated + f"\n\n[... 内容已截断；完整内容见 artifact 文件 ...]"

    def _store_artifact(self, session_id: str, turn_number: int,
                        user_content: str, assistant_content: str,
                        tool_calls: Optional[List[Dict[str, Any]]] = None,
                        tool_results: Optional[List[Dict[str, Any]]] = None,
                        reasoning: str = "",
                        attachments: Optional[List[Dict[str, Any]]] = None,
                        raw_event_refs: Optional[List[Dict[str, Any]]] = None,
                        source_files: Optional[List[str]] = None,
                        completeness: Optional[Dict[str, Any]] = None) -> Path:
        """将完整 payload 写入 artifact 文件，返回文件路径"""
        artifact_dir = self.config.data_dir / "capture_artifacts" / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"turn_{turn_number}.md"
        structured = {
            "tool_calls": tool_calls or [],
            "tool_results": tool_results or [],
            "reasoning": reasoning or "",
            "attachments": attachments or [],
            "raw_event_refs": raw_event_refs or [],
            "source_files": source_files or [],
            "completeness": completeness or {},
        }
        content = f"""# Capture Artifact

- session_id: {session_id}
- turn_number: {turn_number}
- captured_at: {datetime.now().isoformat()}

---

## User

{user_content}

---

## Assistant

{assistant_content}

---

## Structured Capture

````json
{json.dumps(structured, ensure_ascii=False, indent=2, sort_keys=True, default=str)}
````
"""
        path.write_text(content, encoding="utf-8")
        return path

    def _store_reasoning_artifact(self, session_id: str, turn_number: int, reasoning: str) -> Path:
        """按 SyncEngine 相同路径保存 reasoning artifact，保证 hash 投影稳定。"""
        artifact_dir = self.config.data_dir / "capture_artifacts" / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"turn_{turn_number}_reasoning.md"
        content = "\n".join([
            "# Reasoning Artifact",
            "",
            f"- session_id: {session_id}",
            f"- turn_number: {turn_number}",
            f"- captured_at: {datetime.now().isoformat()}",
            "",
            "---",
            "",
            reasoning,
            "",
        ])
        path.write_text(content, encoding="utf-8")
        return path

    def _normalize_list(self, value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def close(self):
        """关闭持久连接和 worker_pool"""
        if hasattr(self, '_sync_pool'):
            self._sync_pool.close()
        if hasattr(self, 'queue') and self.queue is not None:
            try:
                self.queue.close()
            except Exception:
                pass
        if hasattr(self, 'worker_pool') and self.worker_pool is not None:
            try:
                self.worker_pool.close()
            except Exception:
                pass

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
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        reasoning: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        raw_event_refs: Optional[List[Dict[str, Any]]] = None,
        source_files: Optional[List[str]] = None,
        completeness: Optional[Dict[str, Any]] = None,
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

        metadata = dict(metadata or {})
        tool_calls = self._normalize_list(tool_calls if tool_calls is not None else metadata.get("tool_calls"))
        tool_results = self._normalize_list(tool_results if tool_results is not None else metadata.get("tool_results"))
        reasoning = reasoning or metadata.get("reasoning", "")
        attachments = self._normalize_list(attachments if attachments is not None else metadata.get("attachments"))
        raw_event_refs = self._normalize_list(raw_event_refs if raw_event_refs is not None else metadata.get("raw_event_refs"))
        source_files = [str(p) for p in self._normalize_list(source_files if source_files is not None else metadata.get("source_files"))]
        completeness = dict(completeness or metadata.get("completeness") or {})

        # 截断前保存原始内容，用于计算 full_content_hash
        original_user = user_content
        original_assistant = assistant_content

        # 大小限制与完整性策略
        total_bytes = len(user_content.encode("utf-8")) + len(assistant_content.encode("utf-8"))
        capture_mode = "full"
        artifact_path = None

        if total_bytes > self.max_payload_bytes:
            # 超大 payload：先写完整 artifact，再截断 payload 保留摘要
            artifact_path = self._store_artifact(
                session_id, turn_number, user_content, assistant_content,
                tool_calls=tool_calls,
                tool_results=tool_results,
                reasoning=reasoning,
                attachments=attachments,
                raw_event_refs=raw_event_refs,
                source_files=source_files,
                completeness=completeness,
            )
            max_assistant = self.max_payload_bytes - len(user_content.encode("utf-8")) - 1000
            if max_assistant < 5000:
                # user_content 本身已占满配额，两端都保留摘要
                user_content = self._truncate_with_marker(user_content, self.max_payload_bytes // 4)
                assistant_content = self._truncate_with_marker(assistant_content, self.max_payload_bytes // 2)
                capture_mode = "artifact_summary"
            else:
                # payload 保留 assistant 头部摘要
                assistant_content = self._truncate_with_marker(assistant_content, max_assistant)
                capture_mode = "artifact"
            total_bytes = len(user_content.encode("utf-8")) + len(assistant_content.encode("utf-8"))

        reasoning_mode = self.config.get("capture.reasoning_mode", "artifact_summary")
        payload_reasoning = reasoning
        if reasoning:
            metadata["reasoning_sha256"] = hashlib.sha256(reasoning.encode("utf-8")).hexdigest()[:16]
            if reasoning_mode == "artifact_summary":
                reasoning_artifact = metadata.get("reasoning_artifact_path")
                if not reasoning_artifact:
                    reasoning_artifact = str(self._store_reasoning_artifact(session_id, turn_number, reasoning))
                    metadata["reasoning_artifact_path"] = reasoning_artifact
                completeness["reasoning"] = "artifact"
                payload_reasoning = ""

        metadata["capture_mode"] = capture_mode
        metadata["tool_calls"] = tool_calls
        metadata["tool_results"] = tool_results
        metadata["attachments"] = attachments
        metadata["raw_event_refs"] = raw_event_refs
        metadata["source_files"] = source_files
        metadata["completeness"] = completeness
        if reasoning and reasoning_mode != "artifact_summary":
            metadata["reasoning"] = reasoning
        if artifact_path:
            metadata["artifact_path"] = str(artifact_path)

        # 计算 content_hash（截断后，保持与现有 sync_log 兼容）
        model_tag = model or source_agent
        content_hash = compute_content_hash(
            user_content=user_content,
            assistant_content=assistant_content,
            turn_number=turn_number,
            model_tag=model_tag,
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning=payload_reasoning,
            attachments=attachments,
            metadata=metadata,
        )
        # 计算 full_content_hash（原始完整内容，防止截断后去重误判）
        full_content_hash = compute_content_hash(
            user_content=original_user,
            assistant_content=original_assistant,
            turn_number=turn_number,
            model_tag=model_tag,
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning=payload_reasoning,
            attachments=attachments,
            metadata=metadata,
        )
        metadata["full_content_hash"] = full_content_hash

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
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "reasoning": payload_reasoning,
            "attachments": attachments,
            "raw_event_refs": raw_event_refs,
            "source_files": source_files,
            "completeness": completeness,
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
                tool_calls=turn.get("tool_calls"),
                tool_results=turn.get("tool_results"),
                reasoning=turn.get("reasoning", ""),
                attachments=turn.get("attachments"),
                raw_event_refs=turn.get("raw_event_refs"),
                source_files=turn.get("source_files"),
                completeness=turn.get("completeness"),
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
            conn = self._sync_pool.get_conn()
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
