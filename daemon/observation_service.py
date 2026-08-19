# -*- coding: utf-8 -*-
"""Observation engine daemon service helper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict


# daemon 不应触发全量提取：无状态时用最近 24 小时作为增量起点
_DEFAULT_FALLBACK_HOURS = 24


def _resolve_since(engine) -> datetime:
    """根据 store 状态决定增量提取起点，避免 fallback 到全量扫描。"""
    stats = engine.get_store_stats()
    latest = stats.get("latest_update")
    if latest:
        try:
            parsed = datetime.fromisoformat(latest)
            # 确保带 tzinfo，便于后续比较
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc) - timedelta(hours=_DEFAULT_FALLBACK_HOURS)


def run_service(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Run the L3 observation engine incrementally when store state allows."""
    result: Dict[str, Any] = {
        "observations": 0,
        "dimensions": 0,
        "errors": 0,
        "processed_items": 0,
        "status": "skipped",
        "reason": "not_run",
    }
    try:
        from core.cognitive.observation_engine import (
            ObservationEngine,
            canonical_raw_engine_kwargs,
        )
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("observation.enabled", True):
            result["reason"] = "observation_disabled"
            return result
        if not cfg.get("daemon.services.observation_engine", True):
            result["reason"] = "daemon_service_disabled"
            return result

        wiki_dir = str(cfg.wiki_dir)
        engine = ObservationEngine(
            wiki_dir=wiki_dir,
            **canonical_raw_engine_kwargs(cfg),
        )

        since = _resolve_since(engine)
        batch = engine.run_incremental(since=since, persist=True)
        observation_total = batch.total_observations

        result["observations"] = observation_total
        result["dimensions"] = len(batch.dimension_counts)
        result["processed_items"] = int(getattr(batch, "source_count", 0) or 0)
        result["status"] = str(getattr(batch, "extraction_status", "") or "ok")
        result["reason"] = str(getattr(batch, "extraction_reason", "") or "observations_extracted")
        if observation_total and log_info is not None:
            log_info(
                "[DAEMON] Observation Engine 提取 %d 条观察，覆盖 %d 个维度",
                result["observations"],
                result["dimensions"],
            )
        elif not observation_total and log_info is not None:
            log_info(
                "[DAEMON] Observation Engine 0 条观察，status=%s reason=%s processed_items=%d",
                result["status"],
                result["reason"],
                result["processed_items"],
            )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("observation_engine", exc)
        result["errors"] += 1
        result["status"] = "error"
        result["reason"] = type(exc).__name__
    return result
