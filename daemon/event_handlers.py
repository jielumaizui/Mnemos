# -*- coding: utf-8 -*-
"""Daemon event handler implementations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def _run_session_observation(
    cfg: Any,
    session_start_time: Any,
    log_service_error: Callable[[str, Exception], None],
    log_debug: Callable[..., None] | None,
) -> None:
    """session.end 触发增量 Observation。"""
    try:
        from core.cognitive.observation_engine import (
            ObservationEngine,
            canonical_raw_engine_kwargs,
        )

        wiki_dir = str(cfg.wiki_dir)
        engine = ObservationEngine(
            wiki_dir=wiki_dir,
            **canonical_raw_engine_kwargs(cfg),
        )
        since = None
        if session_start_time:
            try:
                since = datetime.fromisoformat(session_start_time)
            except (ValueError, TypeError):
                pass
        # session.end 触发增量提取；无有效时间时回退到 24h 前，避免全量扫描
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
        engine.run_incremental(since=since, persist=True)
        if log_debug is not None:
            log_debug("[DAEMON] session.end 触发增量 Observation 完成")
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("session_end_observation", exc)


def _publish_reflection_completed(
    event_bus: Any,
    result: Any,
    route: Any,
) -> None:
    """发布 reflection.completed 事件。"""
    if event_bus is None:
        return
    event_bus.publish(
        "reflection.completed",
        payload={
            "triggered": result.triggered,
            "route": route.to_dict(),
            "record_id": result.record.id if result.record else None,
            "insight_summary": result.insight.summary if result.insight else "",
            "feedback_messages": result.feedback_messages,
        },
    )


def _run_session_reflection(
    cfg: Any,
    last_user_message: str,
    get_reflection_engine: Callable[[], Any],
    event_bus_provider: Callable[[], Any],
    log_service_error: Callable[[str, Exception], None],
    log_info: Callable[..., None] | None,
) -> None:
    """session.end 触发自动 Reflection。"""
    try:
        from core.reflection.reflection_router import ReflectionRouter

        router = ReflectionRouter()
        route = router.route(last_user_message)
        if not route.should_reflect:
            return
        engine = get_reflection_engine()
        result = engine.reflect_on_user_input(last_user_message)
        if not result.triggered:
            result = engine.reflect_manually(last_user_message)
        _publish_reflection_completed(event_bus_provider(), result, route)
        if log_info is not None:
            log_info("[DAEMON] session.end 自动 Reflection: %s", route.reason)
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("session_end_reflection", exc)


def _publish_feedback_prompt(
    cfg: Any,
    get_reflection_engine: Callable[[], Any],
    event_bus_provider: Callable[[], Any],
    log_service_error: Callable[[str, Exception], None],
) -> None:
    """session.end 触发待反馈提示。"""
    try:
        engine = get_reflection_engine()
        pending = engine.get_pending_feedback(
            hours_since=cfg.get("feedback.pending_hours", 24),
            limit=cfg.get("feedback.pending_limit", 10),
        )
        event_bus = event_bus_provider()
        if pending and event_bus is not None:
            event_bus.publish(
                "feedback.prompt_due",
                payload={
                    "pending_count": len(pending),
                    "reflection_ids": [record.id for record in pending],
                    "trigger": "session_end",
                },
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("session_end_feedback", exc)


def on_session_end(
    event: Any,
    *,
    get_reflection_engine: Callable[[], Any],
    event_bus_provider: Callable[[], Any],
    log_service_error: Callable[[str, Exception], None],
    log_info: Callable[..., None] | None = None,
    log_debug: Callable[..., None] | None = None,
) -> None:
    """Handle session.end by running observation, reflection, and feedback checks."""
    try:
        from core.config import get_config

        cfg = get_config()
        payload = event.payload if isinstance(event.payload, dict) else {}
        last_user_message = payload.get("last_user_message", "")
        session_start_time = payload.get("session_start_time")

        if cfg.get("observation.enabled", True):
            _run_session_observation(cfg, session_start_time, log_service_error, log_debug)

        if cfg.get("reflection.enabled", True) and cfg.get(
            "reflection.auto_trigger_on_session_end", True
        ):
            _run_session_reflection(
                cfg,
                last_user_message,
                get_reflection_engine,
                event_bus_provider,
                log_service_error,
                log_info,
            )

        if cfg.get("feedback.enabled", True):
            _publish_feedback_prompt(
                cfg, get_reflection_engine, event_bus_provider, log_service_error
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("on_session_end", exc)


def _load_observations(
    observation_ids: list, log_service_error: Callable[[str, Exception], None]
) -> list:
    """根据 ID 列表加载 Observation，忽略不存在项。"""
    try:
        from core.cognitive.observation_store import ObservationStore

        store = ObservationStore()
        return [
            store.get_by_id(obs_id)
            for obs_id in observation_ids
            if store.get_by_id(obs_id) is not None
        ]
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("load_observations", exc)
        return []


def _filter_significant_observations(observations: list, threshold: float) -> list:
    """过滤出高置信度或重要类型的 Observation，并排除 feedback_loop 来源。"""
    significant_types = {"deviation", "trend", "contrast"}
    return [
        obs
        for obs in observations
        if obs.source_id != "feedback_loop"
        and (obs.confidence >= threshold or obs.observation_type.value in significant_types)
    ]


def _build_observation_query(significant: list) -> str:
    """把显著观察拼接成 Reflection 查询文本。"""
    lines = ["基于最新观察进行反思："]
    for obs in significant[:5]:
        value_str = str(obs.value)
        if len(value_str) > 100:
            value_str = value_str[:100] + "..."
        line = f"- [{obs.dimension.value}] {obs.observation_type.value}: {value_str}"
        if obs.unit:
            line += f" {obs.unit}"
        line += f" (置信度 {obs.confidence:.2f})"
        lines.append(line)
    return "\n".join(lines)


def _publish_observation_reflection(
    event_bus: Any,
    result: Any,
    significant: list,
) -> None:
    """发布 observation.updated 触发的 reflection.completed 事件。"""
    if event_bus is None or result.record is None:
        return
    event_bus.publish(
        "reflection.completed",
        payload={
            "triggered": result.triggered,
            "trigger_source": "observation.updated",
            "record_id": result.record.id,
            "insight_summary": result.insight.summary if result.insight else "",
            "observation_ids": [obs.id for obs in significant],
        },
    )


def on_observation_updated(
    event: Any,
    *,
    get_reflection_engine: Callable[[], Any],
    event_bus_provider: Callable[[], Any],
    log_service_error: Callable[[str, Exception], None],
    log_info: Callable[..., None] | None = None,
) -> None:
    """Trigger reflection when significant observations are updated."""
    try:
        from core.config import get_config
        from core.reflection.models import ReflectionTrigger

        cfg = get_config()
        if not cfg.get("reflection.enabled", True):
            return
        if not cfg.get("reflection.observation_trigger_enabled", True):
            return

        payload = event.payload if isinstance(event.payload, dict) else {}
        observation_ids = payload.get("observation_ids", [])
        if not observation_ids:
            return

        threshold = float(cfg.get("reflection.observation_trigger_confidence", 0.7))
        observations = _load_observations(observation_ids, log_service_error)
        significant = _filter_significant_observations(observations, threshold)
        if not significant:
            return

        from core.ops.runtime_flow_telemetry import (
            record_runtime_consumed,
            record_runtime_produced,
        )

        for observation in significant:
            record_runtime_produced(
                "observation_to_reflection_and_persona",
                source="core/cognitive/observation_engine.py",
                item_id=str(observation.id),
                intended_consumers=["core/reflection/mirror_engine.py"],
                metadata={"transition": "significant_observation_dispatched"},
                config_or_path=cfg,
            )

        query = _build_observation_query(significant)
        engine = get_reflection_engine()
        result = engine.reflect_on_user_input(query)
        if not result.triggered:
            result = engine.reflect_manually(query, trigger=ReflectionTrigger.OBSERVATION_UPDATED)

        for observation in significant:
            record_runtime_consumed(
                "observation_to_reflection_and_persona",
                source="core/reflection/mirror_engine.py",
                item_id=str(observation.id),
                metadata={
                    "transition": "reflection_terminal",
                    "triggered": bool(result.triggered),
                },
                config_or_path=cfg,
            )

        _publish_observation_reflection(event_bus_provider(), result, significant)
        if log_info is not None:
            log_info(
                "[DAEMON] observation.updated 触发 Reflection: triggered=%s, observations=%d",
                result.triggered,
                len(significant),
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("on_observation_updated", exc)


def on_knowledge_stale(
    event: Any,
    *,
    log_service_error: Callable[[str, Exception], None],
    log_info: Callable[..., None] | None = None,
    log_debug: Callable[..., None] | None = None,
) -> None:
    """Refresh stale wiki pages when a knowledge_stale event arrives."""
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("daemon.services.freshness_refresh", True):
            return
        if not cfg.get("freshness_refresh.auto_refresh_on_stale", True):
            return

        payload = event.payload if isinstance(event.payload, dict) else {}
        wiki_pages = payload.get("wiki_pages", [])
        if not wiki_pages:
            return

        from core.app.freshness_refresh_worker import FreshnessRefreshWorker

        worker = FreshnessRefreshWorker(wiki_base=str(cfg.wiki_dir))
        limit = int(cfg.get("freshness_refresh.auto_refresh_limit", 3))

        refreshed = 0
        skipped = 0
        errors = 0
        for page_path in wiki_pages[:limit]:
            try:
                refresh_result = worker.refresh_page(page_path)
                if refresh_result.status == "refreshed":
                    refreshed += 1
                elif refresh_result.status == "skipped":
                    skipped += 1
                else:
                    errors += 1
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                errors += 1
                if log_debug is not None:
                    log_debug(
                        "[DAEMON] knowledge_stale 自动刷新失败 %s",
                        page_path,
                        exc_info=True,
                    )

        if log_info is not None:
            log_info(
                "[DAEMON] knowledge_stale 自动刷新完成: refreshed=%d skipped=%d errors=%d",
                refreshed,
                skipped,
                errors,
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("on_knowledge_stale", exc)
