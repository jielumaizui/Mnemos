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
- 不做任何 L1 storage 写入
- 返回 < 200ms
- 队列满返回 backpressure
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Protocol, Tuple, TYPE_CHECKING
from urllib.parse import quote

from core.config import get_config
from core.evidence.artifact_capture import (
    build_capture_artifact_refs,
    build_full_capture_artifact_content,
    build_reasoning_artifact_content,
    require_capture_turn_number,
    write_managed_capture_artifact,
)
from core.sync_framework.capture_duplicate_policy import (
    CaptureDuplicatePolicy,
    CaptureDuplicatePolicyError,
)
from core.sync_framework.sync_engine import compute_content_hash
from core.sync_framework.raw_event_store import compute_logical_event_id
from core.sync_framework.agent_source import TURN_STRUCTURED_METADATA_KEYS

# Constants extracted from magic numbers
MAX_PAYLOAD_BYTES = 200000
CAPTURE_SERVICE_CAPTURE_TURN_MAX_ASSISTANT = 5000
_SYSTEM_OWNED_CAPTURE_METADATA_KEYS = frozenset(
    {
        "artifact_path",
        "artifact_refs",
        "capture_artifact_sha256",
        "capture_mode",
        "cognitive_capture_event_id",
        "cognitive_queue_event_id",
        "full_content_hash",
        "logical_event_id",
        "raw_event_id",
        "raw_event_status",
        "reasoning_artifact_path",
        "reasoning_sha256",
    }
)
_SYSTEM_OWNED_COMPLETENESS_KEYS = frozenset(
    {
        "artifact_path",
        "artifact_refs_count",
    }
)

if TYPE_CHECKING:
    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_worker import CaptureWorkerPool

logger = logging.getLogger(__name__)


class _RawStore(Protocol):
    def close(self) -> None: ...

    def upsert_turn(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        model_tag: str = "",
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[list[Dict[str, Any]]] = None,
        tool_results: Optional[list[Dict[str, Any]]] = None,
        reasoning: str = "",
        attachments: Optional[list[Dict[str, Any]]] = None,
        raw_event_refs: Optional[list[Dict[str, Any]]] = None,
        source_files: Optional[list[str]] = None,
        source_path: Optional[str] = None,
        completeness: Optional[Dict[str, Any]] = None,
        content_hash: Optional[str] = None,
        full_content_hash: Optional[str] = None,
        origin: str = "sync_engine",
    ) -> str: ...

    def get_logical_event_id(self, event_id: str) -> str: ...


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
            if self._initialized:  # type: ignore[has-type]
                # 参数变化检测：允许更新 queue 和 worker_pool（单例模式下传入新实例时生效）
                if queue is not None and queue is not getattr(self, "queue", None):
                    self.queue = queue
                if worker_pool is not None and worker_pool is not getattr(
                    self, "worker_pool", None
                ):
                    self.worker_pool = worker_pool
                    self.sync_engine = worker_pool.engine

                # 如果已经初始化过，但之前没启动 worker，现在需要启动
                if not hasattr(self, "sync_engine") and hasattr(self, "worker_pool"):
                    self.sync_engine = self.worker_pool.engine
                if start_worker and hasattr(self, "worker_pool") and not self.worker_pool._running:
                    self.worker_pool.start()
                return
            self._initialized = True

            # 延迟导入避免循环导入
            from core.sync_framework.capture_queue import CaptureQueue
            from core.sync_framework.capture_worker import CaptureWorkerPool

            self.config = get_config()
            self.queue = queue or CaptureQueue(
                db_path=str(Path(self.config.database_dir) / "capture_queue.db")
            )
            self.worker_pool = worker_pool or CaptureWorkerPool(
                queue=self.queue,
                config=self.config,
            )
            self.sync_engine = self.worker_pool.engine
            self.max_payload_bytes = self.config.get("capture.max_payload_bytes", MAX_PAYLOAD_BYTES)
            self.raw_store: Optional[_RawStore] = None
            # Formal Capture is a Raw-first producer.  Retaining a user-selectable
            # raw_event_store bypass would reintroduce queue/persona/handoff
            # states that cannot be reconciled to evidence.
            self.raw_store_enabled = True
            try:
                from core.sync_framework.raw_event_store import RawEventStore

                self.raw_store = RawEventStore(config=self.config)
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
                logger.warning("[CaptureService] raw_event_store 初始化失败", exc_info=True)

            # 启动 worker 池（consumer 进程才启动；MCP producer 传 start_worker=False）
            if start_worker:
                self.worker_pool.start()
            # Retention and artifact cleanup are explicit maintenance work,
            # never a CaptureService constructor effect.

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
        return truncated + "\n\n[... 内容已截断；完整内容见 artifact 文件 ...]"

    def _store_artifact(
        self,
        source_agent: str,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        reasoning: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        raw_event_refs: Optional[List[Dict[str, Any]]] = None,
        source_files: Optional[List[str]] = None,
        completeness: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """将完整 payload 写入 artifact 文件，返回文件路径"""
        structured = {
            "tool_calls": tool_calls or [],
            "tool_results": tool_results or [],
            "reasoning": reasoning or "",
            "attachments": attachments or [],
            "raw_event_refs": raw_event_refs or [],
            "source_files": source_files or [],
            "completeness": completeness or {},
        }
        content = build_full_capture_artifact_content(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            user_content=user_content,
            assistant_content=assistant_content,
            structured=structured,
        )
        return write_managed_capture_artifact(
            database_dir=Path(self.config.database_dir),
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="capture",
            content=content,
        )

    def _store_reasoning_artifact(
        self,
        source_agent: str,
        session_id: str,
        turn_number: int,
        reasoning: str,
    ) -> Path:
        """按 SyncEngine 相同路径保存 reasoning artifact，保证 hash 投影稳定。"""
        content = build_reasoning_artifact_content(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            reasoning=reasoning,
        )
        return write_managed_capture_artifact(
            database_dir=Path(self.config.database_dir),
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="reasoning",
            content=content,
        )

    def _normalize_list(self, value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def close(self):
        """关闭持久连接和 worker_pool"""
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
                sqlite3.Error,
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        if hasattr(self, "worker_pool") and self.worker_pool is not None:
            try:
                self.worker_pool.close()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        if hasattr(self, "raw_store") and self.raw_store is not None:
            try:
                self.raw_store.close()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

    @classmethod
    def reset_instance(cls) -> None:
        """关闭当前单例并清理类级状态，供 RuntimeContext 或测试使用。"""
        instance = cls._instance
        if instance is not None:
            try:
                instance.close()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                logger.warning("[CaptureService] 关闭单例失败", exc_info=True)
            finally:
                cls._instance = None
                cls._initialized = False

    def _validate_capture_inputs(
        self,
        source_agent: str,
        session_id: str,
        turn_number: Any,
    ) -> Optional[Dict[str, Any]]:
        """Fail before filesystem/Raw/queue effects when capture identity is invalid."""
        if (
            not isinstance(source_agent, str)
            or not source_agent.strip()
            or not isinstance(session_id, str)
            or not session_id.strip()
        ):
            return {"status": "error", "message": "source_agent and session_id required"}
        try:
            require_capture_turn_number(turn_number)
        except ValueError as exc:
            return {"status": "error", "duplicate": False, "message": str(exc)}
        return None

    def _normalize_capture_inputs(
        self,
        metadata: Optional[Dict[str, Any]],
        tool_calls: Optional[List[Dict[str, Any]]],
        tool_results: Optional[List[Dict[str, Any]]],
        reasoning: str,
        attachments: Optional[List[Dict[str, Any]]],
        raw_event_refs: Optional[List[Dict[str, Any]]],
        source_files: Optional[List[str]],
        completeness: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """归一化 capture_turn 的输入字段，优先使用显式参数，fallback 到 metadata。"""
        metadata = {
            key: value
            for key, value in dict(metadata or {}).items()
            if key not in _SYSTEM_OWNED_CAPTURE_METADATA_KEYS
        }
        tool_calls = self._normalize_list(
            tool_calls if tool_calls is not None else metadata.get("tool_calls")
        )
        tool_results = self._normalize_list(
            tool_results if tool_results is not None else metadata.get("tool_results")
        )
        reasoning = reasoning or metadata.get("reasoning", "")
        attachments = self._normalize_list(
            attachments if attachments is not None else metadata.get("attachments")
        )
        raw_event_refs = self._normalize_list(
            raw_event_refs if raw_event_refs is not None else metadata.get("raw_event_refs")
        )
        source_files = [
            str(p)
            for p in self._normalize_list(
                source_files if source_files is not None else metadata.get("source_files")
            )
        ]
        completeness = {
            key: value
            for key, value in dict(
                completeness or metadata.get("completeness") or {}
            ).items()
            if key not in _SYSTEM_OWNED_COMPLETENESS_KEYS
        }
        for key in TURN_STRUCTURED_METADATA_KEYS:
            metadata.pop(key, None)
        return {
            "metadata": metadata,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "reasoning": reasoning,
            "attachments": attachments,
            "raw_event_refs": raw_event_refs,
            "source_files": source_files,
            "completeness": completeness,
        }

    def _apply_payload_size_policy(
        self,
        source_agent: str,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        completeness: Dict[str, Any],
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        reasoning: str,
        attachments: List[Dict[str, Any]],
        raw_event_refs: List[Dict[str, Any]],
        source_files: List[str],
    ) -> Tuple[str, str, str, Optional[Path], Dict[str, Any]]:
        """根据 payload 大小决定是否写入 artifact 并截断内容。"""
        total_bytes = len(user_content.encode("utf-8")) + len(assistant_content.encode("utf-8"))
        capture_mode = "full"
        artifact_path = None

        if total_bytes > self.max_payload_bytes:
            artifact_path = self._store_artifact(
                source_agent,
                session_id,
                turn_number,
                user_content,
                assistant_content,
                tool_calls=tool_calls,
                tool_results=tool_results,
                reasoning=reasoning,
                attachments=attachments,
                raw_event_refs=raw_event_refs,
                source_files=source_files,
                completeness=completeness,
            )
            max_assistant = self.max_payload_bytes - len(user_content.encode("utf-8")) - 1000
            if max_assistant < CAPTURE_SERVICE_CAPTURE_TURN_MAX_ASSISTANT:
                user_content = self._truncate_with_marker(user_content, self.max_payload_bytes // 4)
                assistant_content = self._truncate_with_marker(
                    assistant_content, self.max_payload_bytes // 2
                )
                capture_mode = "artifact_summary"
            else:
                assistant_content = self._truncate_with_marker(assistant_content, max_assistant)
                capture_mode = "artifact"
            total_bytes = len(user_content.encode("utf-8")) + len(assistant_content.encode("utf-8"))
            completeness.setdefault("visible_text", capture_mode)
            completeness["truncated"] = True
        else:
            completeness.setdefault("visible_text", "full")
            completeness.setdefault("truncated", False)

        return user_content, assistant_content, capture_mode, artifact_path, completeness

    def _prepare_reasoning_for_capture(
        self,
        reasoning: str,
        metadata: Dict[str, Any],
        completeness: Dict[str, Any],
        source_agent: str,
        session_id: str,
        turn_number: int,
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """处理 reasoning：按需写入 artifact 并返回 payload 中使用的 reasoning。"""
        payload_reasoning = reasoning
        if reasoning:
            metadata["reasoning_sha256"] = hashlib.sha256(
                reasoning.encode("utf-8")
            ).hexdigest()
            if self.config.get("capture.reasoning_mode", "artifact_summary") == "artifact_summary":
                reasoning_artifact = metadata.get("reasoning_artifact_path")
                if not reasoning_artifact:
                    reasoning_artifact = str(
                        self._store_reasoning_artifact(
                            source_agent,
                            session_id,
                            turn_number,
                            reasoning,
                        )
                    )
                    metadata["reasoning_artifact_path"] = reasoning_artifact
                completeness["reasoning"] = "artifact"
                payload_reasoning = ""
        return payload_reasoning, metadata, completeness

    def _build_artifact_refs(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        artifact_path: Optional[Path],
        metadata: Dict[str, Any],
        tool_results: List[Dict[str, Any]],
        attachments: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build stable artifact references without exposing local paths in the URI."""
        return build_capture_artifact_refs(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            capture_artifact_path=artifact_path or "",
            reasoning_artifact_path=str(metadata.get("reasoning_artifact_path") or ""),
            reasoning_sha256=str(metadata.get("reasoning_sha256") or ""),
            tool_results=tool_results,
            attachments=attachments,
            managed_database_dir=Path(self.config.database_dir),
        )

    def _compute_capture_hashes(
        self,
        user_content: str,
        assistant_content: str,
        original_user: str,
        original_assistant: str,
        turn_number: int,
        model_tag: str,
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        payload_reasoning: str,
        attachments: List[Dict[str, Any]],
        metadata: Dict[str, Any],
        completeness: Dict[str, Any],
    ) -> Tuple[str, str]:
        """计算截断后的 content_hash 与原始 full_content_hash；截断时优先使用后者去重。"""
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
        if completeness.get("truncated"):
            content_hash = full_content_hash
        return content_hash, full_content_hash

    def _map_enqueue_status(self, status: str) -> Dict[str, Any]:
        """将 CaptureQueue.enqueue 的返回状态映射为对外状态字典。"""
        if status == "backpressure":
            return {"status": "backpressure", "duplicate": False}
        if status == "duplicate":
            return {"status": "duplicate", "duplicate": True}
        if status == "queued":
            return {"status": "queued", "duplicate": False}
        return {"status": "error", "message": "enqueue failed"}

    def _record_raw_event(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        model_tag: str,
        timestamp: Optional[str],
        metadata: Dict[str, Any],
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        reasoning: str,
        attachments: List[Dict[str, Any]],
        raw_event_refs: List[Dict[str, Any]],
        source_files: List[str],
        completeness: Dict[str, Any],
        content_hash: str,
        full_content_hash: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """写入 canonical raw store；返回 event_id 或阻断正式 capture 的错误。"""
        if not getattr(self, "raw_store_enabled", True):
            return None, None
        store = self.raw_store
        if store is None:
            return None, "raw_event_store unavailable"
        try:
            return (
                store.upsert_turn(
                    source_agent=source_agent,
                    session_id=session_id,
                    turn_number=turn_number,
                    user_content=user_content,
                    assistant_content=assistant_content,
                    model_tag=model_tag,
                    timestamp=timestamp,
                    metadata=dict(metadata or {}),
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    reasoning=reasoning,
                    attachments=attachments,
                    raw_event_refs=raw_event_refs,
                    source_files=source_files,
                    source_path=source_files[0] if source_files else None,
                    completeness=dict(completeness or {}),
                    content_hash=content_hash,
                    full_content_hash=full_content_hash,
                    origin="capture_service",
                ),
                None,
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            logger.warning("[CaptureService] raw_event_store 写入失败", exc_info=True)
            return None, f"{type(exc).__name__}: {exc}"

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
        _replay_generation: int = 0,
    ) -> Dict[str, Any]:
        """
        单轮对话上报入口。

        Returns:
            {"status": "queued" | "duplicate" | "backpressure" | "error",
             "duplicate": bool}
        """
        start_time = time.time()

        validation_error = self._validate_capture_inputs(
            source_agent,
            session_id,
            turn_number,
        )
        if validation_error:
            return validation_error
        if (
            isinstance(_replay_generation, bool)
            or not isinstance(_replay_generation, int)
            or _replay_generation < 0
        ):
            return {
                "status": "error",
                "duplicate": False,
                "message": "replay_generation must be non-negative",
            }

        user_content = user_content or ""
        assistant_content = assistant_content or ""

        normalized = self._normalize_capture_inputs(
            metadata,
            tool_calls,
            tool_results,
            reasoning,
            attachments,
            raw_event_refs,
            source_files,
            completeness,
        )
        metadata = normalized["metadata"]
        tool_calls = normalized["tool_calls"]
        tool_results = normalized["tool_results"]
        reasoning = normalized["reasoning"]
        attachments = normalized["attachments"]
        raw_event_refs = normalized["raw_event_refs"]
        source_files = normalized["source_files"]
        completeness = normalized["completeness"]
        if turn_id:
            # MCP/API capture calls provide this as the native turn/message
            # identifier.  It is propagated to Raw before identity resolution;
            # a generic metadata `id` is never promoted automatically.
            metadata.setdefault("native_event_id", str(turn_id))

        # 截断前保存原始内容，用于计算 full_content_hash
        original_user = user_content
        original_assistant = assistant_content
        raw_completeness = dict(completeness or {})

        user_content, assistant_content, capture_mode, artifact_path, completeness = (
            self._apply_payload_size_policy(
                source_agent,
                session_id,
                turn_number,
                user_content,
                assistant_content,
                completeness,
                tool_calls,
                tool_results,
                reasoning,
                attachments,
                raw_event_refs,
                source_files,
            )
        )
        if artifact_path:
            completeness["artifact_path"] = str(artifact_path)

        payload_reasoning, metadata, completeness = self._prepare_reasoning_for_capture(
            reasoning,
            metadata,
            completeness,
            source_agent,
            session_id,
            turn_number,
        )
        artifact_refs = self._build_artifact_refs(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            artifact_path=artifact_path,
            metadata=metadata,
            tool_results=tool_results,
            attachments=attachments,
        )

        metadata["capture_mode"] = capture_mode
        metadata["artifact_refs"] = artifact_refs
        metadata["capture_artifact_sha256"] = next(
            (
                str(ref.get("sha256") or "")
                for ref in artifact_refs
                if ref.get("artifact_type") == "capture_artifact"
            ),
            "",
        )
        completeness["artifact_refs_count"] = len(artifact_refs)
        if artifact_path:
            metadata["artifact_path"] = str(artifact_path)

        model_tag = model or source_agent
        content_hash, full_content_hash = self._compute_capture_hashes(
            user_content=user_content,
            assistant_content=assistant_content,
            original_user=original_user,
            original_assistant=original_assistant,
            turn_number=turn_number,
            model_tag=model_tag,
            tool_calls=tool_calls,
            tool_results=tool_results,
            payload_reasoning=payload_reasoning,
            attachments=attachments,
            metadata=metadata,
            completeness=completeness,
        )
        metadata["full_content_hash"] = full_content_hash

        raw_event_id, raw_event_error = self._record_raw_event(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
            user_content=original_user,
            assistant_content=original_assistant,
            model_tag=model_tag,
            timestamp=timestamp,
            metadata={
                **(metadata or {}),
                "capture_mode": "canonical_raw",
                "full_content_hash": full_content_hash,
            },
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning=reasoning,
            attachments=attachments,
            raw_event_refs=raw_event_refs,
            source_files=source_files,
            completeness=raw_completeness,
            content_hash=content_hash,
            full_content_hash=full_content_hash,
        )
        if raw_event_id is None:
            self.queue.record_raw_write_failure(
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn_number,
                content_hash=content_hash,
                error=raw_event_error or "raw_event_store write failed",
            )
            return {
                "status": "error",
                "duplicate": False,
                "message": "canonical raw event write failed",
                "raw_event_status": "failed",
            }
        source_event_id = raw_event_id
        metadata["raw_event_id"] = raw_event_id
        raw_store = self.raw_store
        if raw_store is None:
            raise RuntimeError("canonical Raw store disappeared after a successful receipt")
        try:
            metadata["logical_event_id"] = raw_store.get_logical_event_id(raw_event_id)
        except (AttributeError, RuntimeError, sqlite3.Error):
            # A test double may only implement the write receipt.  The native
            # branch remains deterministic; a real RawEventStore always
            # supplies the authoritative alias above.
            metadata["logical_event_id"] = compute_logical_event_id(
                source_agent,
                session_id,
                turn_number,
                native_event_id=str(metadata.get("native_event_id") or ""),
            )
        metadata["raw_event_status"] = "recorded"
        try:
            capture_identity = CaptureDuplicatePolicy.build(
                source_agent=source_agent,
                raw_revision_id=raw_event_id,
                replay_generation=_replay_generation,
            )
        except CaptureDuplicatePolicyError as exc:
            return {"status": "error", "duplicate": False, "message": str(exc)}
        dedupe_key = capture_identity.value
        from core.ops.cognitive_data_contract import stable_event_id

        capture_event_id = stable_event_id(
            "raw_capture", source_agent, raw_event_id, str(_replay_generation)
        )
        queue_event_id = stable_event_id(
            "capture_queue", source_agent, raw_event_id, str(_replay_generation)
        )
        metadata["cognitive_capture_event_id"] = capture_event_id
        metadata["cognitive_queue_event_id"] = queue_event_id

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

        status = self.queue.enqueue(
            source_agent=source_agent,
            session_id=session_id,
            turn_id=turn_id,
            turn_number=turn_number,
            payload=payload,
            content_hash=content_hash,
            raw_revision_id=raw_event_id,
            replay_generation=_replay_generation,
        )

        if status == "queued":
            from core.ops.cognitive_data_contract import (
                CognitiveDataEvent,
                now_utc,
                stable_dedupe_key,
            )
            from core.ops.runtime_flow_telemetry import (
                record_cognitive_data_consumed,
                record_cognitive_data_event,
            )

            subject = f"{source_agent}:raw:{raw_event_id}:generation:{_replay_generation}"
            capture_event = CognitiveDataEvent(
                event_id=capture_event_id,
                source_id=source_event_id,
                asset_id=raw_event_id,
                source_kind="raw_capture",
                source_uri=(
                    f"capture://{quote(source_agent, safe='')}/"
                    f"{quote(session_id, safe='')}/turn/{turn_number}"
                ),
                content_hash=content_hash,
                canonical_subject=subject,
                data_type="conversation_turn",
                producer="capture_service",
                intended_consumers=("capture_queue",),
                privacy_level="local",
                confidence=1.0,
                evidence_refs=(raw_event_id,),
                dedupe_key=stable_dedupe_key("raw_capture", subject, content_hash),
                created_at=now_utc(),
                retention_policy="raw_retention",
            )
            queue_event = CognitiveDataEvent(
                event_id=queue_event_id,
                source_id=capture_event_id,
                asset_id=raw_event_id,
                source_kind="capture_queue",
                source_uri=(
                    f"capture-queue://{quote(source_agent, safe='')}/"
                    f"{quote(session_id, safe='')}/turn/{turn_number}"
                ),
                content_hash=content_hash,
                canonical_subject=subject,
                data_type="queued_capture_event",
                producer="capture_queue",
                intended_consumers=("capture_worker",),
                privacy_level="local",
                confidence=1.0,
                evidence_refs=(capture_event_id,),
                dedupe_key=stable_dedupe_key("capture_queue", subject, content_hash),
                created_at=now_utc(),
                retention_policy="queue_retention",
            )
            record_cognitive_data_event(capture_event, config_or_path=self.config)
            record_cognitive_data_consumed(
                capture_event_id,
                consumer_id="capture_queue",
                outcome="capture_event_queued",
                config_or_path=self.config,
            )
            record_cognitive_data_event(queue_event, config_or_path=self.config)

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            "[CaptureService] capture_turn %s/%s/turn%s -> %s (%.1fms)",
            source_agent,
            session_id,
            turn_number,
            status,
            elapsed_ms,
        )

        result = self._map_enqueue_status(status)
        result.update(
            {
                "raw_event_id": raw_event_id,
                "raw_event_status": "recorded",
                "source_event_id": source_event_id,
                "provenance_id": raw_event_id,
                "capture_dedupe_key": dedupe_key,
            }
        )
        return result

    def replay_turn(
        self,
        *,
        replay_generation: int,
        source_agent: str,
        session_id: str,
        **capture_kwargs: Any,
    ) -> Dict[str, Any]:
        """Request an auditable downstream replay of one canonical Raw turn.

        The underlying Raw revision is intentionally reused.  A caller cannot
        obtain replay semantics by silently resubmitting a normal capture;
        every replay must advance an explicit positive generation.
        """
        try:
            CaptureDuplicatePolicy.require_explicit_replay_generation(replay_generation)
        except CaptureDuplicatePolicyError as exc:
            return {"status": "error", "duplicate": False, "message": str(exc)}
        return self.capture_turn(
            source_agent=source_agent,
            session_id=session_id,
            _replay_generation=replay_generation,
            **capture_kwargs,
        )

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

        nonzero_outcomes = sum(1 for count in (queued, duplicate, backpressure, error) if count > 0)
        if nonzero_outcomes > 1 and (backpressure > 0 or error > 0):
            status = "partial"
        elif backpressure > 0:
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
            "item_receipts": results,
        }

    def end_session(
        self,
        source_agent: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """
        标记 session 结束，异步触发 flush。
        只写标记，不阻塞等待 backend 写入，确保 < 200ms 返回。
        """
        logger.info("[CaptureService] end_session %s/%s", source_agent, session_id)
        try:
            receipt = self.queue.mark_session_end(source_agent, session_id)
            result = receipt.to_dict()
            result["message"] = "session end durably recorded; flush handoff pending"
            return result
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            logger.error(
                "[CaptureService] end_session 持久化失败 %s/%s: %s",
                source_agent,
                session_id,
                exc,
                exc_info=True,
            )
            return {
                "status": "error",
                "receipt_id": "",
                "source_agent": source_agent,
                "session_id": session_id,
                "error": str(exc),
                "message": "session end was not durably recorded",
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
        handoff = self.queue.get_distillation_handoff(source_agent, session_id)
        session_end = self.queue.get_session_end_receipt(source_agent, session_id)
        return {
            "status": record.get("status"),
            "source_agent": source_agent,
            "session_id": session_id,
            "turn_number": record.get("turn_number"),
            "retry_count": record.get("retry_count", 0),
            "created_at": record.get("created_at"),
            "processed_at": record.get("processed_at"),
            "error": record.get("error"),
            "input_revision": handoff.get("input_revision", ""),
            "handoff_receipt_id": handoff.get("receipt_id", ""),
            "handoff_status": handoff.get("status", ""),
            "downstream_receipt_id": handoff.get("downstream_receipt_id", ""),
            "session_end_receipt_id": session_end.get("receipt_id", ""),
            "session_end_status": session_end.get("status", ""),
        }

    def get_pending_counts(self) -> Dict[str, Any]:
        """获取 capture_queue pending 总量和按来源分布，用于状态面板/MCP 诊断。"""
        return {
            "total": self.queue.get_pending_count(),
            "by_source": self.queue.get_pending_counts_by_source(),
        }
