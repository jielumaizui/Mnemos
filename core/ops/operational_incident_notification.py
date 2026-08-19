"""Notification adapters for canonical operational incidents."""

from __future__ import annotations

from typing import Any


class DialogReminderIncidentNotificationAdapter:
    """Project incident status into the existing dialog-reminder channel."""

    def __init__(self, queue: Any | None = None):
        """Bind a queue; production may inject an exact database path."""

        if queue is None:
            from core.kia.dialog_reminder import DialogReminderQueue

            queue = DialogReminderQueue()
        self._queue = queue

    def deliver(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        """Enqueue one idempotent status-only reminder and return its receipt ref."""

        incident_id = str(payload.get("incident_id") or "").strip()
        report_id = str(payload.get("report_id") or "").strip()
        if not incident_id or not report_id:
            raise ValueError("incident notification binding is incomplete")
        if idempotency_key != f"notify-{incident_id}":
            raise ValueError("incident notification idempotency key mismatch")
        root_status = str(payload.get("root_cause_status") or "investigating")
        root_code = str(payload.get("root_cause_code") or "root_cause_unresolved")
        content = (
            f"运行事故 `{incident_id}` 的诊断状态为 `{root_status}`。\n"
            f"诊断报告：`{report_id}`；分类：`{root_code}`。\n"
            "本通知只报告状态，不会创建经验复盘或写入知识页面。"
        )
        reminder_id = self._queue.enqueue(
            issue_id=incident_id,
            page_path=f"operational-incident:{report_id}",
            severity="high",
            content=content,
            choices=["查看诊断状态", "稍后处理"],
        )
        return f"dialog-reminder:{reminder_id}"


__all__ = ["DialogReminderIncidentNotificationAdapter"]
