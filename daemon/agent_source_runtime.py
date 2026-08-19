"""Daemon integration seam for manifest-owned AgentSource capture services."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, cast

from daemon import agent_source_coverage
from daemon import agent_sync_cursor


def _raw_only_engine_factory(cfg: Any) -> Callable[[], Any]:
    """Build the only engine permitted on production Raw-owner paths."""

    def factory() -> Any:
        from daemon.raw_only_sync_engine import RawOnlySyncEngine

        return RawOnlySyncEngine(config=cfg)

    return factory


def persisted_source_coverage(database_dir: Any) -> dict[str, Any] | None:
    """Load the privacy-safe coverage sidecar for a heartbeat projection."""
    path = agent_source_coverage.coverage_state_path(database_dir)
    coverage = agent_source_coverage.load_source_coverage_state(path)
    return coverage or None


def continuous_sync_limits(raw_sync: Any, cfg: Any | None = None) -> Dict[str, int]:
    """Resolve the current throughput budgets without redefining completeness."""
    if cfg is None:
        from core.config import get_config

        cfg = get_config()
    return cast(Dict[str, int], raw_sync.continuous_sync_limits(cfg))


def sync_dirty_sources(
    dirty_sources: list[str],
    cfg: Any,
    *,
    raw_sync: Any,
    trigger_sync: Callable[..., None],
    log_service_error: Callable[[str, Exception], None],
    log: logging.Logger,
) -> None:
    """Dispatch dirty-source tail work through the shared durable cursor store."""
    trigger_sync(
        dirty_sources,
        cfg,
        continuous_sync_limits=lambda: continuous_sync_limits(raw_sync, cfg),
        cursor_store=agent_sync_cursor.AgentSyncCursorStore(cfg.database_dir),
        log_service_error=log_service_error,
        engine_factory=_raw_only_engine_factory(cfg),
        log=log,
    )


def run_raw_sync(
    *,
    raw_sync: Any,
    log_service_error: Callable[[str, Exception], None],
) -> dict[str, Any]:
    """Run scheduled Raw reconciliation and persist its safe coverage projection."""
    from core.config import get_config

    cfg = get_config()
    coverage_path = agent_source_coverage.coverage_state_path(cfg.database_dir)
    return cast(
        dict[str, Any],
        raw_sync.run_service(
            log_service_error,
            continuous_sync_limits_func=lambda: continuous_sync_limits(raw_sync, cfg),
            cursor_store=agent_sync_cursor.AgentSyncCursorStore(cfg.database_dir),
            previous_source_coverage=agent_source_coverage.load_source_coverage_state(coverage_path),
            coverage_state_sink=lambda coverage: agent_source_coverage.write_source_coverage_state(
                coverage_path,
                coverage,
            ),
            engine_factory=_raw_only_engine_factory(cfg),
        ),
    )
