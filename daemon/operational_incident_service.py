"""Daemon worker for diagnosis and incident notification outboxes."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def run_service(config: Any, *, limit: int = 20) -> dict[str, Any]:
    """Advance bounded diagnostic and notification work for one daemon tick."""

    from core.kia.dialog_reminder import DialogReminderQueue
    from core.ops.operational_incident import (
        DialogReminderIncidentNotificationAdapter,
        OperationalIncidentStore,
    )
    from core.ops.operational_incident_reconcile import ingest_pending_incident_artifacts

    database_dir = Path(config.database_dir)
    ingest = ingest_pending_incident_artifacts(database_dir, limit=limit)
    store = OperationalIncidentStore(database_dir / "operational_incidents.db")
    diagnosed = 0
    delivered = 0
    retries = 0
    for _ in range(max(1, int(limit))):
        result = store.diagnose_next()
        if result is None:
            break
        diagnosed += 1
    adapter = DialogReminderIncidentNotificationAdapter(
        DialogReminderQueue(
            db_path=str(database_dir / "dialog_reminder.db"),
        )
    )
    for _ in range(max(1, int(limit))):
        result = store.dispatch_next_notification(adapter)
        if result is None:
            break
        if result["status"] == "delivered":
            delivered += 1
            continue
        retries += 1
        break
    return {
        "status": "ok" if retries == 0 and ingest["failed"] == 0 else "degraded",
        "pending_ingest_committed": ingest["committed"],
        "pending_ingest_failed": ingest["failed"],
        "diagnosed": diagnosed,
        "notifications_delivered": delivered,
        "notification_retries": retries,
    }


__all__ = ["run_service"]
