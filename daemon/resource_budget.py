# -*- coding: utf-8 -*-
"""Resource-budget gates for daemon service scheduling."""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("mnemos.daemon")

_SERVICE_ALIASES = {
    "distill_and_merge": "distill",
    "operational_incidents": "distill",
    "eventbus": "event_bus",
    "inbox_scanner": "inbox_scan",
    "persona_analyzer": "persona_analysis",
    "persona_challenge": "persona_scan",
    "scheduler_tick": "kia_sched",
    "signal_collector": "signal_collect",
}


def budget_name(service_name: str) -> str:
    """Return the ResourceBudget service name for a daemon service."""
    return _SERVICE_ALIASES.get(service_name, service_name)


def deferral(service_name: str) -> Optional[Dict[str, Any]]:
    """Return a deferred result when resources are tight; fail open on telemetry errors."""
    resolved_name = budget_name(service_name)
    try:
        from core.resource_budget import get_budget

        budget = get_budget()
        if budget.can_run(resolved_name):
            return None

        delay = budget.throttle_delay(resolved_name)
        status = budget.status()
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        logger.warning(
            "[DAEMON] 资源预算检查失败，继续调度服务 %s",
            service_name,
            exc_info=True,
        )
        return None

    retry_after = max(1, int(delay or 60))
    return {
        "status": "deferred",
        "reason": "resource_budget",
        "budget_service": resolved_name,
        "resource_state": status.get("state", "unknown"),
        "resource_status": status,
        "retry_after_seconds": retry_after,
        "_meta": {
            "duration_sec": 0.0,
            "timestamp": time.time(),
        },
    }


def defer_if_needed(
    service_name: str,
    now: float,
    interval: float,
    last_run: Dict[str, float],
    service_results: Dict[str, Dict[str, Any]],
    config: Any | None = None,
) -> bool:
    """Record a resource-budget deferral and return True when scheduling should stop."""
    result = deferral(service_name)
    if result is None:
        return False

    retry_after = float(result.get("retry_after_seconds", interval) or interval)
    last_run[service_name] = now - interval + retry_after
    service_results[service_name] = result
    if config is not None:
        from core.ops.runtime_flow_telemetry import (
            record_runtime_consumed,
            record_runtime_produced,
            runtime_item_id,
        )

        budget_item_id = runtime_item_id("resource-budget", service_name, int(now))
        record_runtime_produced(
            "resource_budget_to_scheduler",
            source="core/resource_budget.py",
            item_id=budget_item_id,
            intended_consumers=["daemon/resource_budget.py"],
            metadata={
                "transition": "resource_pressure_detected",
                "resource_state": result.get("resource_state", "unknown"),
            },
            config_or_path=config,
        )
        record_runtime_consumed(
            "resource_budget_to_scheduler",
            source="daemon/resource_budget.py",
            item_id=budget_item_id,
            metadata={"transition": "service_deferred", "service": service_name},
            config_or_path=config,
        )
    logger.info(
        "[DAEMON] 服务 %s 因资源预算延后: state=%s retry_after=%ss",
        service_name,
        result.get("resource_state", "unknown"),
        result.get("retry_after_seconds", retry_after),
    )
    return True
