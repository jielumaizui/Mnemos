"""Idempotent database bootstrap for daemon and deployment checks."""

from __future__ import annotations

import contextlib
import io
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


def _run_step(name: str, func: Callable[[Any], Dict[str, Any] | None], config) -> Dict[str, Any]:
    try:
        payload = func(config) or {}
        return {"name": name, "ok": True, **payload}
    except (ImportError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        logger.warning("[db_init] %s failed: %s", name, exc, exc_info=True)
        return {"name": name, "ok": False, "error": str(exc)}


def _ensure_dirs(config) -> Dict[str, Any]:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.database_dir.mkdir(parents=True, exist_ok=True)
    return {
        "data_dir": str(config.data_dir),
        "database_dir": str(config.database_dir),
    }


def _ensure_sync_log(config) -> Dict[str, Any]:
    from scripts import migrate_db

    db_path = Path(config.database_dir) / "sync_log.db"
    with contextlib.redirect_stdout(io.StringIO()):
        migrate_db.migrate(db_path)
    return {"db_path": str(db_path), "schema_version": migrate_db.SCHEMA_VERSION}


def _ensure_capture_queue(config) -> Dict[str, Any]:
    """Run Capture's explicit schema owner during installation/bootstrap."""
    from core.sync_framework.capture_schema import CaptureQueueSchema

    db_path = Path(config.database_dir) / "capture_queue.db"
    result = CaptureQueueSchema.initialize(db_path)
    return {"db_path": str(db_path), "schema_version": result["schema_version"]}


def _ensure_sync_engine(_config) -> Dict[str, Any]:
    from core.sync_framework.sync_engine import SyncEngine

    engine = SyncEngine()
    try:
        return {"db_path": str(engine.db_path)}
    finally:
        engine.close()


def _ensure_event_bus(config) -> Dict[str, Any]:
    from core.mnemos_bus import EventBus

    bus = EventBus()
    try:
        return {"db_path": str(Path(config.database_dir) / "events.db")}
    finally:
        bus.close()


def _ensure_amphora(config) -> Dict[str, Any]:
    from core.kia import amphora

    amphora._DB_PATH = Path(config.database_dir) / "distill_queue.db"
    amphora._init_db()
    return {"db_path": str(Path(config.database_dir) / "distill_queue.db")}


def _ensure_adaptive_scorer(config) -> Dict[str, Any]:
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

    db_path = Path(config.database_dir) / "mnemos.db"
    AdaptiveScorerV2.ensure_tables(str(db_path))
    return {"db_path": str(db_path)}


def _ensure_operational_incidents(config) -> Dict[str, Any]:
    """Initialize the machine-incident store at the explicit bootstrap boundary."""

    from core.ops.operational_incident import (
        SCHEMA_VERSION,
        initialize_operational_incident_schema,
    )

    db_path = Path(config.database_dir) / "operational_incidents.db"
    initialize_operational_incident_schema(db_path)
    return {"db_path": str(db_path), "schema_version": SCHEMA_VERSION}


BOOTSTRAP_STEPS: tuple[tuple[str, Callable[[Any], Dict[str, Any] | None]], ...] = (
    ("directories", _ensure_dirs),
    ("sync_log_schema", _ensure_sync_log),
    ("capture_queue", _ensure_capture_queue),
    ("sync_engine", _ensure_sync_engine),
    ("event_bus", _ensure_event_bus),
    ("amphora", _ensure_amphora),
    ("adaptive_scorer", _ensure_adaptive_scorer),
    ("operational_incidents", _ensure_operational_incidents),
)


def bootstrap_schema(config: Any | None = None) -> Dict[str, Any]:
    """Create current Mnemos runtime schemas without copying historical table designs."""
    if config is None:
        from core.config import get_config

        config = get_config()

    steps = [_run_step(name, func, config) for name, func in BOOTSTRAP_STEPS]
    ok = all(step["ok"] for step in steps)
    return {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "steps": steps,
    }


def schema_status(config: Any | None = None) -> Dict[str, Any]:
    """Return lightweight schema bootstrap status for scripts and health checks."""
    if config is None:
        from core.config import get_config

        config = get_config()

    return {
        "database_dir": str(config.database_dir),
        "expected_steps": [name for name, _ in BOOTSTRAP_STEPS],
    }
