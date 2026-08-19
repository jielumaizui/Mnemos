# -*- coding: utf-8 -*-
"""AdaptiveConfig daemon service helper."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict

from core.db_utils import sqlite_conn


def run_service(
    get_adaptive_config: Callable[[], Any],
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    log_debug: Callable[..., None] | None = None,
) -> Dict[str, int]:
    """Collect adaptive metrics, roll back bad changes, and apply suggestions."""
    result = {"rollback": 0, "applied": 0, "suggested": 0, "recorded": 0}
    try:
        adaptive_config = get_adaptive_config()
        if adaptive_config is None:
            return result

        collect_metrics(adaptive_config, result, log_debug=log_debug)
        adaptive_config.check_and_rollback()
        adaptive_config.refresh_metrics_from_db()

        suggestions = adaptive_config.suggest_adjustments()
        result["suggested"] = len(suggestions)
        if suggestions:
            applied = adaptive_config.apply_adjustments(suggestions)
            result["applied"] = len(applied)
            if applied and log_info is not None:
                log_info(
                    "[DAEMON] AdaptiveConfig 应用 %d 项配置调整: %s",
                    len(applied),
                    list(applied.keys()),
                )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("adaptive_config", exc)
    return result


def _record_single_metric(
    adaptive_config: Any,
    result: Dict[str, int],
    db_path: Path,
    queries: list,
    feature: str,
    metric: str,
    transform: Callable[[list], float],
    log_debug: Callable[..., None] | None,
) -> None:
    """Execute queries on a single DB and record one transformed metric."""
    if not db_path.exists():
        return
    try:
        with sqlite_conn(str(db_path), timeout=5) as conn:
            rows = [conn.execute(q).fetchone() for q in queries]
            value = transform(rows)
            adaptive_config.record_usage(feature, metric, value)
            result["recorded"] += 1
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
        sqlite3.Error,
    ):
        if log_debug is not None:
            log_debug(f"[DAEMON] 采集 {feature}.{metric} 失败", exc_info=True)


def _record_filesystem_metric(
    adaptive_config: Any,
    result: Dict[str, int],
    path: Path,
    feature: str,
    metric: str,
    transform: Callable[[Path], float],
    log_debug: Callable[..., None] | None,
) -> None:
    """Record a metric derived from a filesystem artifact."""
    if not path.exists():
        return
    try:
        adaptive_config.record_usage(feature, metric, transform(path))
        result["recorded"] += 1
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        if log_debug is not None:
            log_debug(f"[DAEMON] 采集 {feature}.{metric} 失败", exc_info=True)


def collect_metrics(
    adaptive_config: Any,
    result: Dict[str, int],
    *,
    log_debug: Callable[..., None] | None = None,
) -> None:
    """Collect system metrics into AdaptiveConfig usage metrics."""
    from core.config import get_config

    db_dir = get_config().database_dir

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "mnemos.db",
        queries=[
            "SELECT COUNT(*) FROM governed_training_samples AS s "
            "WHERE s.created_at > datetime('now', '-1 hour') AND "
            "(SELECT a.action_type FROM governed_training_sample_actions AS a "
            " WHERE a.sample_id=s.sample_id "
            " ORDER BY a.created_at DESC, a.action_id DESC LIMIT 1)='admit'"
        ],
        feature="scoring",
        metric="feedback_rate",
        transform=lambda rows: 1.0 if (rows[0] and rows[0][0] > 0) else 0.0,
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "mnemos.db",
        queries=[
            "SELECT COUNT(*) FROM search_sessions " "WHERE created_at > datetime('now', '-1 hour')",
            "SELECT COUNT(*) FROM search_sessions "
            "WHERE created_at > datetime('now', '-1 hour') "
            "AND outcome_status = 'no_result'",
        ],
        feature="search",
        metric="no_result_rate",
        transform=lambda rows: (
            (rows[1][0] / rows[0][0]) if (rows[0] and rows[0][0] > 0 and rows[1]) else 0.0
        ),
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "mnemos.db",
        queries=[
            "SELECT COUNT(*) FROM search_sessions " "WHERE created_at > datetime('now', '-1 hour')",
            "SELECT COUNT(*) FROM search_sessions "
            "WHERE created_at > datetime('now', '-1 hour') "
            "AND (clicked_path IS NULL OR clicked_path = '')",
        ],
        feature="app",
        metric="push_ignore_rate",
        transform=lambda rows: (
            (rows[1][0] / rows[0][0]) if (rows[0] and rows[0][0] > 0 and rows[1]) else 0.0
        ),
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "wiki_state.db",
        queries=[
            "SELECT COUNT(*) FROM evolution_alerts "
            "WHERE alert_type IN ('version_outdated', 'context_expired') "
            "AND resolved = 0",
            "SELECT COUNT(DISTINCT entity) FROM evolution_alerts",
        ],
        feature="knowledge_graph",
        metric="stale_page_rate",
        transform=lambda rows: (
            (rows[0][0] / rows[1][0]) if (rows[0] and rows[1] and rows[1][0] > 0) else 0.0
        ),
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "raw_events.db",
        queries=[
            "SELECT COUNT(*) FROM raw_turns",
            "SELECT COUNT(*) FROM raw_turns WHERE completeness_status != 'complete'",
        ],
        feature="raw",
        metric="partial_rate",
        transform=lambda rows: (
            (rows[1][0] / rows[0][0]) if (rows[0] and rows[0][0] > 0 and rows[1]) else 0.0
        ),
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "distill_actions.db",
        queries=[
            "SELECT COUNT(*) FROM distill_action_log "
            "WHERE created_at > datetime('now', '-24 hours')",
            "SELECT COUNT(*) FROM distill_action_log "
            "WHERE created_at > datetime('now', '-24 hours') "
            "AND result_status IN ('skipped', 'failed')",
        ],
        feature="quality_gate",
        metric="rejection_rate",
        transform=lambda rows: (
            (rows[1][0] / rows[0][0]) if (rows[0] and rows[0][0] > 0 and rows[1]) else 0.0
        ),
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "distill_actions.db",
        queries=[
            "SELECT COUNT(*) FROM distill_action_log "
            "WHERE created_at > datetime('now', '-24 hours')",
            "SELECT COUNT(*) FROM distill_action_log "
            "WHERE created_at > datetime('now', '-24 hours') "
            "AND result_status = 'pending_review'",
        ],
        feature="quality_gate",
        metric="review_rate",
        transform=lambda rows: (
            (rows[1][0] / rows[0][0]) if (rows[0] and rows[0][0] > 0 and rows[1]) else 0.0
        ),
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "delivery_events.db",
        queries=[
            "SELECT COUNT(*) FROM delivery_events "
            "WHERE created_at > datetime('now', '-24 hours')",
            "SELECT COUNT(*) FROM delivery_events "
            "WHERE created_at > datetime('now', '-24 hours') "
            "AND feedback IN ('ignore', 'dismiss')",
        ],
        feature="delivery",
        metric="dismiss_rate",
        transform=lambda rows: (
            (rows[1][0] / rows[0][0]) if (rows[0] and rows[0][0] > 0 and rows[1]) else 0.0
        ),
        log_debug=log_debug,
    )

    _record_filesystem_metric(
        adaptive_config,
        result,
        db_dir / "rejected_documents",
        feature="document_process",
        metric="rejection_rate",
        transform=lambda path: min(1.0, len(list(path.glob("*.json"))) / 10.0),
        log_debug=log_debug,
    )

    _record_single_metric(
        adaptive_config,
        result,
        db_dir / "adaptive_config.db",
        queries=[
            "SELECT ewma FROM usage_metrics "
            "WHERE feature = 'distill' AND metric = 'false_positive_rate' "
            "ORDER BY recorded_at DESC LIMIT 1"
        ],
        feature="distill",
        metric="false_positive_rate",
        transform=lambda rows: rows[0][0] if rows[0] else 0.0,
        log_debug=log_debug,
    )
