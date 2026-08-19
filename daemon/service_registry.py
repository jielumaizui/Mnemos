# -*- coding: utf-8 -*-
"""Service name resolution for the Mnemos daemon scheduler."""

from __future__ import annotations

from typing import Any, Callable, Mapping, cast

DIRECT_SERVICE_TARGETS = {
    "capture_worker": "service_capture_worker",
    "heartbeat": "service_heartbeat",
    "inbox_scanner": "service_inbox_scanner",
    "signal_collector": "service_signal_collector",
    "persona_analyzer": "service_persona_analyzer",
    "persona_challenge": "_run_persona_challenge",
    "raw_sync": "service_raw_sync",
    "raw_projection": "service_raw_projection",
    "l1_sync": "service_raw_sync",
    "distill_and_merge": "service_distill_and_merge",
    "distill_cognitive_actions": "service_distill_cognitive_actions",
    "operational_incidents": "service_operational_incidents",
    "wiki_route": "service_wiki_route",
    "eventbus": "service_eventbus_health",
    "startup_compensation": "_run_startup_compensation",
    "drift_report": "_generate_drift_report",
    "preflight_checks": "_run_preflight_checks",
    "scheduler_tick": "service_scheduler_tick",
    "adaptive_config": "service_adaptive_config",
    "search_ignore_detection": "service_search_ignore_detection",
    "user_correction_detection": "service_user_correction_detection",
    "observation_engine": "service_observation_engine",
    "reflection_engine": "service_reflection_engine",
    "feedback_prompt": "service_feedback_prompt",
    "recap_consumption": "service_recap_consumption",
    "cognitive_graph_reconcile": "service_cognitive_graph_reconcile",
    "prediction_maturity": "service_prediction_maturity",
    "training_governance": "service_training_governance",
    "cognitive_consolidation": "service_cognitive_consolidation",
    "dispute_scan": "service_dispute_scan",
    "reminder_scan": "service_reminder_scan",
    "freshness_refresh": "service_freshness_refresh",
    "entropy_scan": "service_entropy_scan",
    "db_maintenance": "service_db_maintenance",
    "agent_path_watch": "service_agent_path_watch",
}

CFG_SERVICE_TARGETS = {
    "retry_failed": "service_retry_failed",
    "trigger_dispatcher": "_sync_trigger_dirty_sources",
    "file_ingestor": "service_file_ingestor",
    "link_probe": "service_link_probe",
}


def resolve_service_call(
    service_name: str,
    namespace: Mapping[str, Any],
    cfg: Any = None,
) -> Callable[[], Any]:
    """Resolve a daemon service key to a no-argument callable."""
    target = DIRECT_SERVICE_TARGETS.get(service_name)
    if target:
        return cast(Callable[[], Any], namespace[target])

    target = CFG_SERVICE_TARGETS.get(service_name)
    if target:
        service = namespace[target]
        return lambda: service(cfg)

    raise ValueError(f"未知服务: {service_name}")
