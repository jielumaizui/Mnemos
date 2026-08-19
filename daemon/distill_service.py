# -*- coding: utf-8 -*-
"""Daemon services for distillation work."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict

_SERVICE_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
    AttributeError,
    RuntimeError,
    sqlite3.Error,
)


def service_distill_and_merge(
    log_service_error: Callable[[str, Exception], None],
) -> Dict[str, Any]:
    """Process Hephaestus tasks through the sole synchronous distill owner."""
    result = {"processed": 0}
    try:
        from core.hephaestus_worker import HephaestusWorker

        worker = HephaestusWorker()
        result["processed"] = worker.process_all()
    except _SERVICE_ERRORS as exc:
        log_service_error("distill_and_merge", exc)
    return result


def service_distill_cognitive_actions(
    log_service_error: Callable[[str, Exception], None],
) -> Dict[str, Any]:
    """Consume queued distill cognitive actions into terminal audit states."""
    try:
        from core.config import get_config
        from core.hephaestus.distill_cognitive_action_worker import (
            DistillCognitiveActionWorker,
        )

        cfg = get_config()
        database_dir = Path(cfg.database_dir)
        limit = int(cfg.get("distill.cognitive_action_worker_limit", 100) or 100)
        worker = DistillCognitiveActionWorker(
            database_dir / "distill_actions.db",
            database_dir=database_dir,
        )
        result = worker.process_queued(limit=limit)
        return {"ok": True, **result}
    except _SERVICE_ERRORS as exc:
        log_service_error("distill_cognitive_actions", exc)
        return {"ok": False, "error": str(exc)}
