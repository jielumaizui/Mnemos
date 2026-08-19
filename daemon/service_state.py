# -*- coding: utf-8 -*-
"""Service execution state helpers for the Mnemos daemon."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any, Callable, Dict

logger = logging.getLogger("mnemos.daemon")


def is_module_missing(exc: Exception) -> bool:
    """Return True when an exception indicates an optional module is unavailable."""
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return True
    message = str(exc).lower()
    return "no module named" in message or "cannot import" in message


def service_error_key(service_name: str) -> str:
    """Map contextual names like raw_sync:codex back to the daemon service key."""
    return service_name.split(":", 1)[0]


def record_service_error(
    error_state: Dict[str, Dict[str, Any]],
    service_name: str,
    exc: Exception,
) -> Dict[str, Any]:
    key = service_error_key(service_name)
    previous = error_state.get(key, {})
    now = datetime.now().isoformat()
    state = {
        "count": int(previous.get("count", 0)) + 1,
        "last_error": str(exc),
        "last_error_type": exc.__class__.__name__,
        "first_error_at": previous.get("first_error_at") or now,
        "last_error_at": now,
        "last_context": service_name,
    }
    error_state[key] = state
    return state


def clear_service_error(
    error_state: Dict[str, Dict[str, Any]],
    service_name: str,
) -> Dict[str, Any] | None:
    """Clear a service's current error state and return the previous error."""
    return error_state.pop(service_error_key(service_name), None)


def log_service_error(
    error_state: Dict[str, Dict[str, Any]],
    service_name: str,
    exc: Exception,
    *,
    log: logging.Logger | None = None,
) -> None:
    """Record and log a daemon service error with severity based on error kind."""
    log = log or logger
    record_service_error(error_state, service_name, exc)
    if is_module_missing(exc):
        log.info("服务 %s 依赖模块不可用: %s", service_name, exc)
    else:
        log.warning("服务 %s 异常: %s", service_name, exc)
        log.debug("服务 %s traceback:\n%s", service_name, traceback.format_exc())


def make_service_done_callback(
    service_name: str,
    *,
    service_futures: Dict[str, Any],
    service_results: Dict[str, Dict[str, Any]],
    error_state: Dict[str, Dict[str, Any]],
    service_enabled: Callable[[Any, str], bool],
    log: logging.Logger | None = None,
):
    """Build a ThreadPool done callback that updates daemon service state."""
    log = log or logger

    def callback(future):
        service_futures.pop(service_name, None)
        try:
            from core.config import get_config

            cfg = get_config()
            result = future.result()
            result = result if isinstance(result, dict) else {}
            result.setdefault("enabled", service_enabled(cfg, service_name))
            service_results[service_name] = {
                "at": datetime.now().isoformat(),
                "ok": True,
                "result": result,
            }
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            record_service_error(error_state, service_name, exc)
            log.error("服务 %s 未捕获异常: %s", service_name, exc, exc_info=True)
            log.debug(traceback.format_exc())
            service_results[service_name] = {
                "at": datetime.now().isoformat(),
                "ok": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    return callback
