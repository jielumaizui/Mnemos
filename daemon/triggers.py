# -*- coding: utf-8 -*-
"""Trigger-driven tail acceleration for the Mnemos continuous Raw owner."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping

from daemon.agent_sync_cursor import AgentSyncCursorStore
from daemon.raw_sync import sync_source_continuously


logger = logging.getLogger("mnemos.daemon")


def sync_dirty_sources(
    dirty_sources: List[str],
    cfg: Any,
    *,
    continuous_sync_limits: Callable[[], Dict[str, int]],
    cursor_store: AgentSyncCursorStore,
    log_service_error: Callable[[str, Exception], None],
    engine_factory: Callable[[], Any] | None = None,
    source_registry: Any = None,
    log: logging.Logger | None = None,
) -> None:
    """Accelerate recent tails without replacing scheduled reconciliation.

    Watcher events deliberately do *not* claim a complete source scan.  The
    scheduled raw_sync service retains ownership of denominator reconciliation.
    """
    del cfg  # The manifest-owned service contract, not a caller latch, controls capture.
    log = log or logger
    log.debug("[DAEMON] sync_trigger_dirty_sources: dirty=%s", dirty_sources)
    if not dirty_sources:
        return

    try:
        if engine_factory is None:
            from core.sync_framework.sync_engine import SyncEngine

            engine_factory = SyncEngine
        if source_registry is None:
            from core.sync_framework.registry import SourceRegistry

            source_registry = SourceRegistry

        limits: Mapping[str, int] = continuous_sync_limits()
        source_map = {source.name: source for source in source_registry.list_sources()}
        engine = engine_factory()

        for name in dirty_sources:
            source = source_map.get(name)
            if source is None:
                log.warning("[DAEMON] dirty source %s not found in registry", name)
                continue
            try:
                outcome = sync_source_continuously(
                    source,
                    engine,
                    cursor_store,
                    limits,
                    include_reconciliation=False,
                )
                for error in outcome["errors"]:
                    log_service_error(f"trigger_raw_sync:{name}", error)
                log.info(
                    "[DAEMON] trigger_raw_sync: %s tail_sessions=%d raw_committed_turns=%d",
                    name,
                    outcome["cursor"]["tail_selected_sessions"],
                    outcome["raw_committed_turns"],
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                log_service_error(f"trigger_raw_sync:{name}", exc)
        engine.close()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("trigger_raw_sync", exc)
