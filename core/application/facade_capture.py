"""Session capture operations shared by the default application facade."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, List


APPLICATION_OPERATION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)


class FacadeCaptureMixin:
    """Translate session capture services into stable facade DTOs."""

    if TYPE_CHECKING:
        _logger: logging.Logger

    def session_save(
        self,
        session_id: str,
        messages: List[Dict],
        tags: List[str] | None = None,
        source_agent: str = "unknown",
    ) -> Dict:
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService(start_worker=False)
            turns = []
            user_content = ""
            assistant_content = ""
            turn_number = 0
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    if assistant_content:
                        turns.append(
                            {
                                "turn_number": turn_number,
                                "user_content": user_content,
                                "assistant_content": assistant_content,
                            }
                        )
                        turn_number += 1
                    user_content = content
                    assistant_content = ""
                elif role == "assistant":
                    assistant_content = content

            if user_content or assistant_content:
                turns.append(
                    {
                        "turn_number": turn_number,
                        "user_content": user_content,
                        "assistant_content": assistant_content,
                    }
                )

            result = service.capture_session(
                source_agent=source_agent,
                session_id=session_id,
                turns=turns,
            )
            return {
                "success": result.get("status") in ("queued", "duplicate", "done"),
                "message": (
                    f"Session 已入队: {result.get('queued_count', 0)} 轮次 queued, "
                    f"{result.get('duplicate_count', 0)} 重复"
                ),
                "session_id": session_id,
                "capture_result": result,
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("会话保存失败: %s", exc)
            return {
                "success": False,
                "message": f"保存失败: {exc}",
            }

    def capture_turn(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_id: str = "",
        turn_number: int = 0,
        user_content: str = "",
        assistant_content: str = "",
        timestamp: str = "",
        model: str = "",
        cwd: str = "",
        metadata: Dict | None = None,
        tool_calls: list | None = None,
        tool_results: list | None = None,
        reasoning: str = "",
        attachments: list | None = None,
        raw_event_refs: list | None = None,
        source_files: list | None = None,
        completeness: Dict | None = None,
    ) -> Dict:
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService(start_worker=False)
            result = service.capture_turn(
                source_agent=source_agent,
                session_id=session_id,
                turn_id=turn_id or None,
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                timestamp=timestamp or None,
                model=model or None,
                cwd=cwd or None,
                metadata=metadata or {},
                tool_calls=tool_calls,
                tool_results=tool_results,
                reasoning=reasoning,
                attachments=attachments,
                raw_event_refs=raw_event_refs,
                source_files=source_files,
                completeness=completeness,
            )
            return {
                "success": result["status"] in ("queued", "duplicate"),
                "status": result["status"],
                "duplicate": result.get("duplicate", False),
                "source_agent": source_agent,
                "session_id": session_id,
                "turn_number": turn_number,
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("capture_turn 失败: %s", exc, exc_info=True)
            return {
                "success": False,
                "status": "error",
                "message": str(exc),
            }

    def capture_session(self, source_agent: str, session_id: str, turns: List[Dict]) -> Dict:
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService(start_worker=False)
            result = service.capture_session(
                source_agent=source_agent,
                session_id=session_id,
                turns=turns,
            )
            return {
                "success": result.get("status") in ("queued", "duplicate"),
                "status": result["status"],
                "queued_count": result.get("queued_count", 0),
                "duplicate_count": result.get("duplicate_count", 0),
                "backpressure_count": result.get("backpressure_count", 0),
                "error_count": result.get("error_count", 0),
                "item_receipts": result.get("item_receipts", []),
                "session_id": session_id,
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("capture_session 失败: %s", exc, exc_info=True)
            return {
                "success": False,
                "status": "error",
                "message": str(exc),
            }

    def end_session(self, source_agent: str, session_id: str) -> Dict:
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService(start_worker=False)
            result = service.end_session(
                source_agent=source_agent,
                session_id=session_id,
            )
            return {
                **result,
                "success": result.get("status") != "error",
                "source_agent": source_agent,
                "session_id": session_id,
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("end_session 失败: %s", exc, exc_info=True)
            return {
                "success": False,
                "status": "error",
                "message": str(exc),
            }

    def capture_status(self, source_agent: str, session_id: str, turn_number: int = -1) -> Dict:
        from core.config import get_config
        from core.sync_framework.capture_service import CaptureService
        from core.sync_framework.capture_status_reader import CaptureStatusReader

        try:
            active_service = CaptureService._instance
            queue_path = getattr(getattr(active_service, "queue", None), "db_path", None)
            reader = CaptureStatusReader(
                queue_path or (get_config().database_dir / "capture_queue.db")
            )
            result = reader.read(
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn_number if turn_number >= 0 else None,
            )
            return {
                "success": True,
                "status": result.get("status"),
                "source_agent": source_agent,
                "session_id": session_id,
                "turn_number": result.get("turn_number"),
                "retry_count": result.get("retry_count", 0),
                "pending_counts": result.get("pending_counts", {"total": 0, "by_source": {}}),
                "error": result.get("error"),
                "input_revision": result.get("input_revision", ""),
                "handoff_receipt_id": result.get("handoff_receipt_id", ""),
                "handoff_status": result.get("handoff_status", ""),
                "downstream_receipt_id": result.get("downstream_receipt_id", ""),
                "session_end_receipt_id": result.get("session_end_receipt_id", ""),
                "session_end_status": result.get("session_end_status", ""),
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("capture_status 失败: %s", exc, exc_info=True)
            return {
                "success": False,
                "status": "error",
                "message": str(exc),
            }

    @staticmethod
    def _messages_to_turns(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        turns: List[Dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                user_content = content
                assistant_content = ""
                if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                    assistant_content = messages[i + 1].get("content", "")
                    i += 2
                else:
                    i += 1
                turns.append(
                    {
                        "turn_number": len(turns),
                        "user_content": user_content,
                        "assistant_content": assistant_content,
                    }
                )
            elif role == "assistant":
                turns.append(
                    {
                        "turn_number": len(turns),
                        "user_content": "",
                        "assistant_content": content,
                    }
                )
                i += 1
            else:
                turns.append(
                    {
                        "turn_number": len(turns),
                        "user_content": "",
                        "assistant_content": "",
                        "raw_event_refs": [{"role": role, "content": content}],
                    }
                )
                i += 1
        return turns
