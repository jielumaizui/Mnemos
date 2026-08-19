# -*- coding: utf-8 -*-
"""Default daemon service intervals."""

from __future__ import annotations

from typing import Dict


def resolve_capture_tick(default: int = 5) -> int:
    """Resolve capture_worker tick interval from config with a safe fallback."""
    try:
        from core.config import get_config

        return int(get_config().get("capture.tick_interval_seconds", default))
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        return default


def build_default_intervals(capture_tick: int | None = None) -> Dict[str, int]:
    """Build the default daemon service interval table."""
    capture_tick = resolve_capture_tick() if capture_tick is None else capture_tick
    return {
        "heartbeat": 60,
        "capture_worker": capture_tick,
        "inbox_scanner": 300,
        "signal_collector": 300,
        "persona_analyzer": 3600,
        "persona_challenge": 3600,
        "raw_sync": 600,
        "raw_projection": 300,
        "retry_failed": 600,
        # trigger_dispatcher 只负责把 watchdog/polling 标记的 dirty source 同步回 L1；
        # 10s 会导致来源一脏就高频扫描（如 opencode 30s 轮询），改成 60s 更合理。
        "trigger_dispatcher": 60,
        "file_ingestor": 300,
        "distill_and_merge": 300,
        "distill_cognitive_actions": 60,
        "operational_incidents": 60,
        "wiki_route": 3600,
        "eventbus": 60,
        "startup_compensation": 300,
        "drift_report": 86400,
        "preflight_checks": 86400,
        "scheduler_tick": 60,
        "adaptive_config": 3600,
        "search_ignore_detection": 300,
        "user_correction_detection": 3600,
        # observation_engine 已改为增量，且被 session.end 事件频繁触发；
        # 降低定时扫描频率，避免持续占用 CPU。
        "observation_engine": 21600,
        "reflection_engine": 86400,
        "feedback_prompt": 86400,
        "recap_consumption": 60,
        "cognitive_graph_reconcile": 3600,
        "prediction_maturity": 3600,
        "training_governance": 3600,
        "cognitive_consolidation": 86400,
        "dispute_scan": 3600,
        "reminder_scan": 86400,
        "freshness_refresh": 86400,
        "entropy_scan": 86400,
        "link_probe": 3600,
        "db_maintenance": 86400,
        "agent_path_watch": 300,
    }


def apply_interval_overrides(intervals: Dict[str, int], cfg: object) -> None:
    """Apply runtime interval overrides to a daemon interval table."""
    for service, key, default in (
        ("capture_worker", "capture.tick_interval_seconds", 5),
        ("observation_engine", "observation.interval_seconds", 3600),
        ("reflection_engine", "reflection.interval_seconds", 86400),
        ("feedback_prompt", "feedback.prompt_interval_seconds", 86400),
        ("recap_consumption", "feedback.recap_consumption_interval_seconds", 60),
        ("raw_projection", "raw_projection.interval_seconds", 300),
        ("distill_and_merge", "distill.tick_interval_seconds", 300),
        (
            "distill_cognitive_actions",
            "distill.cognitive_action_worker_interval_seconds",
            60,
        ),
        (
            "operational_incidents",
            "distill.operational_incident_worker_interval_seconds",
            60,
        ),
        ("wiki_route", "wiki_route.interval_seconds", 3600),
        ("dispute_scan", "dispute_scan.interval_seconds", 3600),
        ("reminder_scan", "reminder.scan_interval_seconds", 86400),
        ("freshness_refresh", "freshness_refresh.interval_seconds", 86400),
        ("entropy_scan", "entropy.scan_interval_seconds", 86400),
        ("link_probe", "link_probe.interval_seconds", 3600),
        (
            "prediction_maturity",
            "prediction.maturity_interval_seconds",
            3600,
        ),
        (
            "training_governance",
            "training_governance.interval_seconds",
            3600,
        ),
        ("cognitive_consolidation", "cognitive_consolidation.interval_seconds", 86400),
        ("agent_path_watch", "watchers.agent_paths.poll_interval_seconds", 300),
    ):
        intervals[service] = int(cfg.get(key, default))  # type: ignore[attr-defined]
    intervals["db_maintenance"] = (
        int(cfg.get("storage.maintenance.interval_hours", 24)) * 3600  # type: ignore[attr-defined]
    )
