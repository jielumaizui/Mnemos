# -*- coding: utf-8 -*-
"""Reflection and feedback daemon service helpers."""

from __future__ import annotations

from typing import Any, Callable, Dict


def run_reflection_engine(
    get_reflection_engine: Callable[[], Any],
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Trigger a scheduled manual reflection pass."""
    result: Dict[str, Any] = {
        "triggered": False,
        "insight_summary": "",
        "feedback_messages": [],
        "errors": 0,
    }
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("reflection.enabled", True):
            return result
        if not cfg.get("daemon.services.reflection_engine", True):
            return result

        query = cfg.get("reflection.manual_query", "分析最近认知与决策模式")
        engine = get_reflection_engine()
        reflection_result = engine.reflect_manually(query)

        result["triggered"] = reflection_result.triggered
        result["insight_summary"] = (
            reflection_result.insight.summary if reflection_result.insight else ""
        )
        result["feedback_messages"] = reflection_result.feedback_messages
        if reflection_result.triggered and log_info is not None:
            log_info(
                "[DAEMON] Reflection Engine 触发: %s",
                result["insight_summary"][:120],
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("reflection_engine", exc)
        result["errors"] += 1
    return result


def run_feedback_prompt(
    event_bus: Any,
    get_reflection_engine: Callable[[], Any],
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    log_debug: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Publish feedback prompt events for pending reflection feedback."""
    result = {"pending_count": 0, "prompted": False, "errors": 0}
    if event_bus is None:
        if log_debug is not None:
            log_debug("[DAEMON] EventBus 未初始化，跳过 feedback prompt")
        return result
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("feedback.enabled", True):
            return result
        if not cfg.get("daemon.services.feedback_prompt", True):
            return result

        engine = get_reflection_engine()
        pending = engine.get_pending_feedback(
            hours_since=cfg.get("feedback.pending_hours", 24),
            limit=cfg.get("feedback.pending_limit", 10),
        )
        result["pending_count"] = len(pending)

        if pending:
            event_bus.publish(
                "feedback.prompt_due",
                payload={
                    "pending_count": len(pending),
                    "reflection_ids": [record.id for record in pending],
                    "trigger": "daemon_feedback_prompt",
                },
            )
            result["prompted"] = True
            if log_info is not None:
                log_info("[DAEMON] Feedback Prompt: %d 条待反馈 Reflection", len(pending))
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("feedback_prompt", exc)
        result["errors"] += 1
    return result
