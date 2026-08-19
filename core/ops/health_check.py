"""Machine-readable operational health report for Mnemos."""

from __future__ import annotations

import argparse
import contextlib
import io
import inspect
import json
import shutil
import sqlite3
import subprocess
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, cast

from core.db_utils import sqlite_artifact_exists
from core.db_utils import sqlite_artifact_size
from core.db_utils import validate_sql_identifier
from core.privacy.redaction import redact_key_source
from core.privacy.redaction import redact_sensitive_data
from core.privacy.redaction import redact_url
from core.vaults.content_audit import audit_vault_content
from core.ops.health_contract import (
    CANONICAL_HEALTH_CHECK_IDS,
    CANONICAL_HEALTH_CHECK_IDS_HASH,
)
from core.ops.heartbeat_health import (
    build_heartbeat_report as _build_heartbeat_report,
    expected_heartbeat_service_ids as _expected_heartbeat_service_ids,
    heartbeat_service_error_active as _heartbeat_service_error_active,
    project_heartbeat_services as _public_heartbeat_services,
    read_iso_datetime as _read_iso_datetime,
    verify_heartbeat_identity as _verify_heartbeat_identity,
)
from core.ops.readiness_query_budget import (
    ReadinessQueryDeadlineExceeded,
    health_readiness_query_budget,
    readiness_query_failure_code,
)

STRICT_HEALTH_CHECKS = (
    "storage",
    "wiki",
    "agent",
    "disk",
    "api",
    "schema",
    "heartbeat",
    "wiki_route",
    "wiki_projection",
    "runtime_producer_consumer",
    "install_lifecycle",
    "amphora",
    "queues",
    "cognitive_readiness",
    "sqlite_disk_budget",
    "model_call_ledger",
)

STATUS_RANK = {
    "ok": 0,
    "warning": 1,
    "skipped": 1,
    "degraded": 2,
    "failed": 3,
    "error": 3,
}
SQLITE_COUNT_WHERE_ALLOWLIST = frozenset({"status = ? AND severity IN (?, ?)"})
_HEALTH_CHECK_FAILURES = (
    OSError,
    sqlite3.Error,
    ValueError,
    TypeError,
    RuntimeError,
    ImportError,
    AttributeError,
    KeyError,
)
HEALTH_COGNITIVE_READINESS_QUERY_TIMEOUT_SECONDS = 10.0
_ACTIVE_HEALTH_READINESS_CACHE: ContextVar[dict[str, Any] | None] = ContextVar(
    "mnemos_health_readiness_cache",
    default=None,
)


def _connect_read_only(db_path: Path):
    return sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=2,
    )


def _item(status: str, **details: Any) -> Dict[str, Any]:
    return {"status": status, **details}


def _config_get(config: Any, key: str, default: Any) -> Any:
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


def summarize_model_api_config(
    api_cfg: Any, kind: str, *, show_sensitive: bool = False
) -> Dict[str, Any]:
    """Return a shared config-only status for model API endpoints."""
    provider = str(getattr(api_cfg, "provider", "") or "")
    base_url = str(getattr(api_cfg, "base_url", "") or "")
    model = str(getattr(api_cfg, "model", "") or "")
    source = str(getattr(api_cfg, "source", "") or "")
    resolver_configured = bool(getattr(api_cfg, "configured", False))
    endpoint_complete = bool(provider and base_url and model)
    configured = bool(resolver_configured and endpoint_complete)
    if configured:
        status = "configured"
    elif resolver_configured:
        status = "incomplete"
    else:
        status = "not_configured"
    return {
        "kind": kind,
        "configured": configured,
        "provider": provider,
        "base_url": base_url if show_sensitive else redact_url(base_url),
        "model": model,
        "source": source if show_sensitive else (redact_key_source(source) if source else ""),
        "status": status,
    }


def _safe_check(name: str, func: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        result = func()
        if "status" not in result:
            result["status"] = "ok"
        return result
    except _HEALTH_CHECK_FAILURES:
        # Health JSON is a public diagnostic payload.  A check can raise an
        # SDK/database exception containing a request body, response body, or
        # credential, so this boundary exposes only a stable typed category.
        return _item(
            "degraded",
            error=f"{name}: check_failed",
            error_category="check_failed",
        )


def _check_result(value: Any) -> Dict[str, Any]:
    return cast(Dict[str, Any], value)


def _call_config_check(
    func: Callable[..., Dict[str, Any]],
    config: Any,
    *,
    show_sensitive: bool,
) -> Dict[str, Any]:
    try:
        return func(config, show_sensitive=show_sensitive)
    except TypeError as exc:
        if "show_sensitive" not in str(exc):
            raise
        return func(config)


def _call_optional_config(func: Callable[..., Dict[str, Any]], config: Any) -> Dict[str, Any]:
    if inspect.signature(func).parameters:
        return func(config)
    return func()


def _sqlite_counts(db_path: Path, table: str, status_col: str = "status") -> Dict[str, int]:
    if not sqlite_artifact_exists(db_path):
        return {}
    try:
        validate_sql_identifier(table)
        validate_sql_identifier(status_col)
        with _connect_read_only(db_path) as conn:
            rows = conn.execute(
                " ".join(
                    [
                        "SELECT",
                        status_col,
                        ", COUNT(*) FROM",
                        table,
                        "GROUP BY",
                        status_col,
                    ]
                )
            ).fetchall()
        return {str(status): int(count) for status, count in rows}
    except sqlite3.Error:
        return {}


def _sqlite_count_where(
    db_path: Path,
    table: str,
    where_sql: str,
    params: tuple[Any, ...],
) -> int:
    if not sqlite_artifact_exists(db_path):
        return 0
    try:
        table = validate_sql_identifier(table)
        if where_sql not in SQLITE_COUNT_WHERE_ALLOWLIST:
            raise ValueError(f"Unsupported health WHERE clause: {where_sql!r}")
        with _connect_read_only(db_path) as conn:
            query = f"SELECT COUNT(*) FROM {table} WHERE {where_sql}"  # nosec B608
            row = conn.execute(
                query,
                params,
            ).fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError):
        return 0


def _config_int(config: Any, key: str, default: int) -> int:
    try:
        value = config.get(key, default)
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError, AttributeError):
        return default


def _age_seconds_from_iso(value: Any) -> int | None:
    timestamp = _read_iso_datetime(value)
    if timestamp is None:
        return None
    now = datetime.now(timestamp.tzinfo) if timestamp.tzinfo else datetime.now()
    return max(0, int((now - timestamp).total_seconds()))


def _processing_clock_value(task: Dict[str, Any]) -> Any:
    latest_raw: Any = None
    latest_ts: datetime | None = None
    for key in ("updated_at", "started_at"):
        raw = task.get(key)
        timestamp = _read_iso_datetime(raw)
        if timestamp is None:
            continue
        comparable = timestamp.replace(tzinfo=None)
        if latest_ts is None or comparable > latest_ts:
            latest_raw = raw
            latest_ts = comparable
    if latest_raw is not None:
        return latest_raw
    raw = task.get("created_at")
    if _read_iso_datetime(raw) is not None:
        return raw
    return latest_raw


def _stale_processing_tasks(
    tasks: list[Dict[str, Any]],
    timeout_minutes: int,
    *,
    sample_limit: int = 10,
) -> list[Dict[str, Any]]:
    threshold = max(1, int(timeout_minutes)) * 60
    stale: list[Dict[str, Any]] = []
    for task in tasks:
        age_seconds = _age_seconds_from_iso(_processing_clock_value(task))
        if age_seconds is None or age_seconds <= threshold:
            continue
        stale.append(
            {
                "task_id": task.get("task_id"),
                "session_id": task.get("session_id"),
                "created_at": task.get("created_at"),
                "started_at": task.get("started_at"),
                "updated_at": task.get("updated_at"),
                "age_seconds": age_seconds,
                "progress_step": task.get("progress_step"),
                # A task's free-form progress detail and provider exception
                # can contain the submitted prompt, response, credential, or
                # caller-controlled error text.  Health needs to expose the
                # stalled state, not a copy of that content.
                "error_category": (
                    "distill_task_processing_error" if task.get("error") else ""
                ),
            }
        )
    stale.sort(key=lambda item: int(item.get("age_seconds") or 0), reverse=True)
    return stale[:sample_limit]


def _check_storage(config) -> Dict[str, Any]:
    from core.diagnostics import ConnectionDiagnostics

    status = ConnectionDiagnostics.check_storage(config)
    ok = bool(status.reachable)
    return _item(
        "ok" if ok else "degraded",
        backend=status.backend,
        configured=status.configured,
        reachable=status.reachable,
        path=status.path,
        error=status.error,
    )


def _check_wiki(config) -> Dict[str, Any]:
    from core.diagnostics import ConnectionDiagnostics

    status = ConnectionDiagnostics.check_wiki(config)
    ok = bool(status.exists and status.writable)
    return _item(
        "ok" if ok else "degraded",
        path=status.path,
        exists=status.exists,
        writable=status.writable,
    )


def _check_agents() -> Dict[str, Any]:
    from core.ops.health_agent import build_agent_health

    return build_agent_health()


def _check_daemon() -> Dict[str, Any]:
    try:
        result = subprocess.run(["pgrep", "-f", "mnemos_daemon"], capture_output=True, text=True)
        pids = [p for p in result.stdout.splitlines() if p.strip()]
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        pids = []
    return _item("ok" if pids else "degraded", running=bool(pids), pids=pids)


def _check_event_bus(config) -> Dict[str, Any]:
    db_path = config.database_dir / "events.db"
    counts = _sqlite_counts(db_path, "events")
    pending = int(counts.get("pending", 0)) + int(counts.get("processing", 0))
    exists = sqlite_artifact_exists(db_path)
    size_bytes, encrypted = sqlite_artifact_size(db_path)
    status = "ok"
    if not exists:
        status = "skipped"
    elif pending > int(config.get("event_bus.queue_depth_alert", 1000) or 1000):
        status = "degraded"
    return _item(
        status,
        db_path=str(db_path),
        exists=exists,
        encrypted_artifact=encrypted,
        size_bytes=size_bytes,
        counts=counts,
        pending_or_processing=pending,
    )


def _check_schema(config) -> Dict[str, Any]:
    from core.db_init import schema_status

    return _check_result(schema_status(config))


def _check_amphora(config=None) -> Dict[str, Any]:
    db_path = Path(config.database_dir) / "distill_queue.db" if config else None
    if db_path is not None and not sqlite_artifact_exists(db_path):
        return _item(
            "skipped",
            db_path=str(db_path),
            error="distillation queue is not initialized",
        )
    try:
        from core.kia import amphora

        pending_all = len(amphora.list_pending(include_future_retry=True))
        pending_ready = len(amphora.list_pending(include_future_retry=False))
        processing_tasks = amphora.list_processing()
        processing = len(processing_tasks)
        processing_stale_timeout_minutes = int(getattr(amphora, "TIMEOUT_MINUTES", 30) or 30)
        stale_processing = _stale_processing_tasks(
            processing_tasks,
            processing_stale_timeout_minutes,
        )
        counts = {
            "pending": pending_all,
            "ready": pending_ready,
            "delayed": max(0, pending_all - pending_ready),
            "processing": processing,
            "done": amphora.get_task_count("done"),
            "committed": amphora.get_task_count("committed"),
            "intentional_skip": amphora.get_task_count("intentional_skip"),
            "proposal_pending": amphora.get_task_count("proposal_pending"),
            "partial": amphora.get_task_count("partial"),
            "retryable_failed": amphora.get_task_count("retryable_failed"),
            "reconciliation_required": amphora.get_task_count("reconciliation_required"),
            "failed": amphora.get_task_count("failed"),
            "archived": amphora.get_task_count("archived"),
        }
        failed = int(counts["failed"])
        reconciliation_required = int(counts["reconciliation_required"])
        if failed > 0 or reconciliation_required > 0:
            return _item(
                "degraded",
                **counts,
                failed_task_budget=0,
                stale_processing=len(stale_processing),
                stale_processing_budget=0,
                stale_processing_tasks=stale_processing,
                processing_stale_timeout_minutes=processing_stale_timeout_minutes,
                error=(
                    "amphora has failed distillation tasks"
                    if failed
                    else "amphora has legacy completion rows requiring reconciliation"
                ),
                repair_action=(
                    "Run `python3 scripts/reconcile_pipeline_receipts.py --apply`, then "
                    "verify its reconciliation_gap is zero."
                    if reconciliation_required
                    else "Inspect with `mnemos distill status` and resolve, archive, "
                    "or retry failed tasks."
                ),
            )
        if stale_processing:
            return _item(
                "degraded",
                **counts,
                failed_task_budget=0,
                stale_processing=len(stale_processing),
                stale_processing_budget=0,
                stale_processing_tasks=stale_processing,
                processing_stale_timeout_minutes=processing_stale_timeout_minutes,
                error="amphora has stale processing distillation tasks",
                repair_action=(
                    "Run `python3 mnemos_cli.py distill reset-timeouts --minutes "
                    f"{processing_stale_timeout_minutes} --json`, then drain or let the "
                    "daemon process the returned pending tasks."
                ),
            )
        return _item(
            "ok",
            **counts,
            failed_task_budget=0,
            stale_processing=0,
            stale_processing_budget=0,
            stale_processing_tasks=[],
            processing_stale_timeout_minutes=processing_stale_timeout_minutes,
        )
    except _HEALTH_CHECK_FAILURES:
        return _item(
            "skipped",
            error="amphora_check_failed",
            error_category="amphora_check_failed",
        )


def _distill_processing_freshness(
    db_path: Path,
    timeout_minutes: int,
    *,
    sample_limit: int = 10,
) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        "processing": 0,
        "stale_processing": 0,
        "stale_processing_budget": 0,
        "processing_stale_timeout_minutes": timeout_minutes,
        "stale_processing_tasks": [],
        "timestamp_columns": [],
    }
    if not sqlite_artifact_exists(db_path):
        details["db_exists"] = False
        return details
    try:
        table = validate_sql_identifier("distillation_tasks")
        with _connect_read_only(db_path) as conn:
            conn.row_factory = sqlite3.Row
            columns = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
            }
            if not columns or "status" not in columns:
                details["table_exists"] = False
                return details
            details["table_exists"] = True
            timestamp_columns = [
                name for name in ("updated_at", "started_at", "created_at") if name in columns
            ]
            details["timestamp_columns"] = timestamp_columns
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE status = ?",  # nosec B608
                ("processing",),
            ).fetchone()
            details["processing"] = int(row[0]) if row else 0
            if not timestamp_columns:
                return details

            selected_columns = [
                name
                for name in (
                    "task_id",
                    "session_id",
                    "created_at",
                    "started_at",
                    "updated_at",
                    "progress_step",
                    "progress_detail",
                    "error",
                )
                if name in columns
            ]
            rows = conn.execute(
                f"""
                SELECT {", ".join(selected_columns)}
                FROM {table}
                WHERE status = ?
                """,  # nosec B608
                ("processing",),
            ).fetchall()
        stale = _stale_processing_tasks(
            [dict(row) for row in rows],
            timeout_minutes,
            sample_limit=sample_limit,
        )
        details["stale_processing"] = len(stale)
        details["stale_processing_tasks"] = stale
        return details
    except sqlite3.Error:
        details["error"] = "distill_processing_health_read_failed"
        details["error_category"] = "distill_processing_health_read_failed"
        return details


def _check_queue_backlog(config) -> Dict[str, Any]:
    database_dir = config.database_dir
    distill_db_path = database_dir / "distill_queue.db"
    distill_counts = _sqlite_counts(distill_db_path, "distillation_tasks")
    recap_counts = _sqlite_counts(database_dir / "recap_tasks.db", "recap_tasks")
    reminder_counts = _sqlite_counts(database_dir / "dialog_reminder.db", "dialog_reminders")

    distill_failed = int(distill_counts.get("failed", 0))
    processing_stale_timeout_minutes = _config_int(
        config,
        "health.queue_budgets.distill_processing_stale_minutes",
        30,
    )
    distill_processing = _distill_processing_freshness(
        distill_db_path,
        processing_stale_timeout_minutes,
    )
    distill_processing_stale = int(distill_processing.get("stale_processing", 0))
    recap_high_pending = _sqlite_count_where(
        database_dir / "recap_tasks.db",
        "recap_tasks",
        "status = ? AND severity IN (?, ?)",
        ("pending", "high", "critical"),
    )
    dialog_pending = int(reminder_counts.get("pending", 0))
    dialog_active = sum(
        int(reminder_counts.get(status, 0)) for status in ("pending", "deferred", "pushed")
    )

    budgets = {
        "distill_failed": _config_int(config, "health.queue_budgets.distill_failed", 0),
        "distill_processing_stale": _config_int(
            config, "health.queue_budgets.distill_processing_stale", 0
        ),
        "recap_high_pending": _config_int(config, "health.queue_budgets.recap_high_pending", 0),
        "dialog_pending": _config_int(config, "health.queue_budgets.dialog_pending", 500),
        "dialog_active": _config_int(config, "health.queue_budgets.dialog_active", 500),
    }
    problems = []
    if distill_failed > budgets["distill_failed"]:
        problems.append("distill_failed")
    if distill_processing_stale > budgets["distill_processing_stale"]:
        problems.append("distill_processing_stale")
    if recap_high_pending > budgets["recap_high_pending"]:
        problems.append("recap_high_pending")
    if dialog_pending > budgets["dialog_pending"]:
        problems.append("dialog_pending")
    if dialog_active > budgets["dialog_active"]:
        problems.append("dialog_active")

    details = {
        "distill": {
            "db_path": str(distill_db_path),
            "counts": distill_counts,
            "failed": distill_failed,
            "failed_budget": budgets["distill_failed"],
            "processing": int(distill_counts.get("processing", 0)),
            "processing_freshness": distill_processing,
        },
        "recap": {
            "db_path": str(database_dir / "recap_tasks.db"),
            "counts": recap_counts,
            "high_or_critical_pending": recap_high_pending,
            "high_or_critical_pending_budget": budgets["recap_high_pending"],
        },
        "dialog_reminder": {
            "db_path": str(database_dir / "dialog_reminder.db"),
            "counts": reminder_counts,
            "pending": dialog_pending,
            "pending_budget": budgets["dialog_pending"],
            "active": dialog_active,
            "active_budget": budgets["dialog_active"],
            "expiration_policy": "mnemos reminder expire-stale --days 30",
        },
        "budgets": budgets,
        "over_budget": problems,
    }
    if problems:
        details.update(
            {
                "status": "degraded",
                "error": "queue backlog exceeds health budgets",
                "repair_actions": [
                    (
                        "python3 mnemos_cli.py distill reset-timeouts --minutes "
                        f"{processing_stale_timeout_minutes} --json"
                    ),
                    "mnemos distill retry-failed --all",
                    "mnemos distill archive-failed --all",
                    "mnemos recap dismiss --all --severity high --reason <reason>",
                    "mnemos reminder expire-stale --days 30",
                ],
            }
        )
        return details
    return _item("ok", **details)


def _check_disk(config) -> Dict[str, Any]:
    target = config.database_dir
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(str(target))
    free_ratio = usage.free / usage.total if usage.total else 0.0
    return _item(
        "ok" if free_ratio >= 0.05 else "degraded",
        path=str(target),
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        free_ratio=round(free_ratio, 4),
    )


def _check_api(config, *, show_sensitive: bool = False) -> Dict[str, Any]:
    from core.llm_config import (
        resolve_embedding_api_config,
        resolve_effective_llm_api_config,
        resolve_reranker_api_config,
    )

    llm = resolve_effective_llm_api_config(config)
    embedding = resolve_embedding_api_config(config)
    reranker = resolve_reranker_api_config(config)
    models = {
        "llm": llm,
        "embedding": embedding,
        "reranker": reranker,
    }
    payload = {}
    for name, cfg in models.items():
        payload[name] = summarize_model_api_config(cfg, name, show_sensitive=show_sensitive)
    all_configured = all(item["status"] == "configured" for item in payload.values())
    return _item(
        "ok" if all_configured else "degraded",
        models=payload,
        note="Deployment requires LLM, embedding, and reranker model endpoints.",
    )


def _check_system_contracts() -> Dict[str, Any]:
    from core.system_contracts import build_contract_health

    return _check_result(build_contract_health())


def _check_module_toggles(config) -> Dict[str, Any]:
    from core.module_toggles import build_module_toggle_health

    return _check_result(build_module_toggle_health(config))


def _check_runtime_producer_consumer(config) -> Dict[str, Any]:
    from core.ops.runtime_flow_health import build_runtime_producer_consumer_health

    return _check_result(build_runtime_producer_consumer_health(config))


def _check_migrations(config) -> Dict[str, Any]:
    from core.migrations.registry import build_migration_health

    return _check_result(build_migration_health(config))


def _check_backup(config) -> Dict[str, Any]:
    from core.backup.snapshot_manager import build_backup_health

    return _check_result(build_backup_health(config))


def _check_data_ownership(config) -> Dict[str, Any]:
    from core.privacy.data_ownership import build_data_ownership_health

    return _check_result(build_data_ownership_health(config))


def _check_model_call_ledger(config) -> Dict[str, Any]:
    """Expose the single provider-boundary ledger through readonly health."""
    from core.ops.model_call_ledger_health import build_model_call_ledger_health

    return build_model_call_ledger_health(config)


def _check_golden_benchmark() -> Dict[str, Any]:
    from core.benchmarks.golden import build_golden_benchmark_health

    return _check_result(build_golden_benchmark_health())


def _check_distill_json_quality(config) -> Dict[str, Any]:
    from core.hephaestus.distillation_metrics import summarize_json_parse_metrics

    return _check_result(summarize_json_parse_metrics(config.database_dir))


def _check_distill_cognitive_actions(config) -> Dict[str, Any]:
    db_path = config.database_dir / "distill_actions.db"
    artifact_dir = config.database_dir / "distill_cognitive_actions"
    report = _item(
        "ok",
        schema_version="mnemos.distill_cognitive_actions_health.v1",
        db_path=str(db_path),
        db_exists=db_path.exists(),
        table_exists=False,
        total_actions=0,
        counts={},
        status_counts={},
        queued_over_budget=False,
        queued_budget=0,
        consumption_count=0,
        artifact_dir=str(artifact_dir),
        artifact_count=0,
        effect_count=0,
        applied_without_effect=0,
        effect_without_action=0,
        effect_lineage_gap_count=0,
        effect_audit_schema_state="missing_database",
    )
    if artifact_dir.exists():
        report["artifact_count"] = sum(1 for _path in artifact_dir.rglob("*.json"))
    if not db_path.exists():
        return report

    try:
        with _connect_read_only(db_path) as conn:
            table_exists = bool(conn.execute("""
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='cognitive_action_log'
                    """).fetchone())
            report["table_exists"] = table_exists
            if not table_exists:
                return report
            action_rows = conn.execute("""
                SELECT cognitive_action, COUNT(*) AS count
                FROM cognitive_action_log
                GROUP BY cognitive_action
                ORDER BY cognitive_action
                """).fetchall()
            status_rows = conn.execute("""
                SELECT status, COUNT(*) AS count
                FROM cognitive_action_log
                GROUP BY status
                ORDER BY status
                """).fetchall()
            consumption_table_exists = bool(conn.execute("""
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='cognitive_action_consumptions'
                    """).fetchone())
            consumption_count = 0
            if consumption_table_exists:
                consumption_count = int(
                    conn.execute("SELECT COUNT(*) FROM cognitive_action_consumptions").fetchone()[0]
                )
    except sqlite3.Error:
        report["status"] = "warning"
        report["error"] = "distill_cognitive_action_health_read_failed"
        report["error_category"] = "distill_cognitive_action_health_read_failed"
        return report

    counts = {str(action): int(count) for action, count in action_rows}
    status_counts = {str(status): int(count) for status, count in status_rows}
    report["counts"] = counts
    report["status_counts"] = status_counts
    report["total_actions"] = sum(counts.values())
    report["consumption_count"] = consumption_count
    from core.hephaestus.cognitive_action_effect_audit import (
        audit_cognitive_action_effects,
    )

    effect_audit = audit_cognitive_action_effects(db_path)
    effect_gaps = dict(effect_audit.get("gaps") or {})
    report["effect_count"] = int(effect_audit.get("counts", {}).get("effects", 0))
    report["applied_without_effect"] = int(
        effect_gaps.get("applied_without_effect", 0)
    )
    report["effect_without_action"] = int(effect_gaps.get("effect_without_action", 0))
    report["effect_audit_schema_state"] = str(effect_audit.get("schema_state") or "")
    report["effect_lineage_gap_count"] = max(
        0,
        int(effect_audit.get("lineage_gap_count", 0))
        - int(effect_gaps.get("nonterminal_commands", 0)),
    )
    queued_budget = int(_config_get(config, "distill.cognitive_actions.queued_budget", 0) or 0)
    queued_count = int(status_counts.get("queued", 0))
    report["queued_budget"] = queued_budget
    report["queued_over_budget"] = queued_count > queued_budget
    if report["queued_over_budget"]:
        report["status"] = "warning"
        report["reason"] = "queued cognitive actions exceed budget"
    if report["effect_lineage_gap_count"]:
        report["status"] = "warning"
        report["reason"] = "cognitive action effects have lineage gaps"
    return report


def _check_wiki_route(config) -> Dict[str, Any]:
    raw_wiki_dir = getattr(config, "wiki_dir", None)
    budgets = {
        "inbox_ready_to_classify": _config_int(
            config, "health.wiki_route_budgets.inbox_ready_to_classify", 100
        ),
        "needs_review_pages": _config_int(
            config, "health.wiki_route_budgets.needs_review_pages", 500
        ),
        "formal_source_prefixed_pages": _config_int(
            config, "health.wiki_route_budgets.formal_source_prefixed_pages", 250
        ),
        "title_basename_collision_groups": _config_int(
            config, "health.wiki_route_budgets.title_basename_collision_groups", 350
        ),
    }
    if not raw_wiki_dir:
        return _item(
            "degraded",
            error="wiki route vault path is not configured",
            budgets=budgets,
            repair_actions=["configure Mnemos wiki_dir before running routing health"],
        )

    wiki_dir = Path(raw_wiki_dir)
    report = audit_vault_content(wiki_dir, sample_limit=5)
    classification = report.get("classification", {})
    counts = {name: int(classification.get(name, 0) or 0) for name in budgets}
    over_budget = [name for name, count in counts.items() if count > budgets[name]]
    result = _item(
        "degraded" if over_budget else "ok",
        schema_version="mnemos.wiki_route_health.v1",
        wiki_dir=str(wiki_dir),
        counts=counts,
        budgets=budgets,
        over_budget=over_budget,
        samples={
            "inbox_ready": classification.get("inbox_ready_samples", []),
            "needs_review": classification.get("needs_review_samples", []),
            "formal_source_prefixed": classification.get("formal_source_prefixed_samples", []),
            "title_collisions": classification.get("title_basename_collisions", []),
        },
        repair_actions=[
            "mnemos vaults audit-content --json",
            "python3 scripts/reorganize_wiki.py --dry-run",
            "mnemos daemon run",
        ],
    )
    if over_budget:
        result["error"] = "wiki route budgets exceeded"
    return result


def _check_wiki_projection(config) -> Dict[str, Any]:
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    db_path = Path(config.database_dir) / "wiki_projection.db"
    if not db_path.exists():
        return _item(
            "degraded",
            schema_version="mnemos.wiki_projection_health.v1",
            error="wiki projection ledger is not initialized",
            projection_gap=-1,
            repair_actions=[
                "python3 scripts/reconcile_wiki_projections.py --scan --apply",
                "python3 scripts/reconcile_wiki_projections.py --json",
            ],
        )
    report = WikiProjectionLedger(db_path).reconciliation_report()
    gap = int(report.get("projection_gap", 0) or 0)
    details = dict(report)
    details["reconciliation_schema_version"] = details.pop("schema_version", "")
    details.update(
        schema_version="mnemos.wiki_projection_health.v1",
        error="" if gap == 0 else "wiki projection consumer receipts are incomplete",
        repair_actions=[]
        if gap == 0
        else [
            "python3 scripts/reconcile_wiki_projections.py --publish --apply --json",
            "python3 scripts/reconcile_wiki_projections.py --json",
        ],
    )
    return _item("ok" if gap == 0 else "degraded", **details)


def _check_install_lifecycle(config) -> Dict[str, Any]:
    from core.setup.install_lifecycle import build_install_lifecycle_health

    return _check_result(build_install_lifecycle_health(config))


def _check_sqlite_disk_budget(config) -> Dict[str, Any]:
    from core.ops.sqlite_disk_budget import build_sqlite_disk_budget_report

    return _check_result(build_sqlite_disk_budget_report(config, update_state=False))


def _check_adaptive_policy(config) -> Dict[str, Any]:
    from core.kia.adaptive_config import AdaptiveConfig
    from core.kia.policy import EffectivePolicy

    db_path = config.database_dir / "adaptive_config.db"
    summary = AdaptiveConfig(
        db_path=db_path,
        policy=EffectivePolicy(
            db_path=db_path,
            config=config,
            initialize=False,
        ),
        initialize=False,
    ).get_policy_summary()
    status = "ok" if summary.get("ok") else "warning"
    return _item(status, **summary)


@contextlib.contextmanager
def _health_cognitive_readiness_scope():
    """Share exactly one bounded readiness report between health subchecks."""
    cache: dict[str, Any] = {"report": None, "failure_code": None}
    with health_readiness_query_budget(HEALTH_COGNITIVE_READINESS_QUERY_TIMEOUT_SECONDS):
        token = _ACTIVE_HEALTH_READINESS_CACHE.set(cache)
        try:
            yield
        finally:
            _ACTIVE_HEALTH_READINESS_CACHE.reset(token)


def _readiness_query_failure(code: str) -> Dict[str, Any]:
    return _item(
        "degraded",
        error=code,
        error_category=code,
        repair_actions=[
            "Retry health after the local readiness database is available.",
            "Use `python3 scripts/audit_cognitive_readiness.py --json --budget` for the unbounded audit.",
        ],
    )


def _active_readiness_failure(cache: dict[str, Any] | None) -> str | None:
    if cache is not None and cache.get("failure_code"):
        return str(cache["failure_code"])
    return readiness_query_failure_code()


def _record_active_readiness_failure(
    cache: dict[str, Any] | None,
    code: str,
) -> Dict[str, Any]:
    if cache is not None:
        cache["failure_code"] = code
    return _readiness_query_failure(code)


def _check_cognitive_learning_from_signal(learning_signal: Dict[str, Any]) -> Dict[str, Any]:

    gaps = {
        "observation_output_gap": int(learning_signal.get("observation_output_gap", 0) or 0),
        "policy_patch_gap": int(learning_signal.get("policy_patch_gap", 0) or 0),
        "consolidation_run_gap": int(learning_signal.get("consolidation_run_gap", 0) or 0),
        "delivery_feedback_lineage_gap": int(
            learning_signal.get("delivery_feedback_lineage_gap", 0) or 0
        ),
        "observation_lineage_gap": int(
            learning_signal.get("observation_lineage_gap", 0) or 0
        ),
        "policy_driver_lineage_gap": int(
            learning_signal.get("policy_driver_lineage_gap", 0) or 0
        ),
        "consolidation_coverage_gap": int(
            learning_signal.get("consolidation_coverage_gap", 0) or 0
        ),
        "required_tables_missing_count": int(
            learning_signal.get("required_tables_missing_count", 0) or 0
        ),
        "required_evidence_empty_count": int(
            learning_signal.get("required_evidence_empty_count", 0) or 0
        ),
        "stale_lineage_count": int(learning_signal.get("stale_lineage_count", 0) or 0),
        "unobserved_lineage_count": int(
            learning_signal.get("unobserved_lineage_count", 0) or 0
        ),
    }
    gap_names = [name for name, count in gaps.items() if count > 0]
    status = "ok" if not gap_names else "warning"
    return _item(
        status,
        schema_version=learning_signal.get("schema_version"),
        gaps=gaps,
        gap_names=gap_names,
        raw_signal_count=learning_signal.get("raw_signal_count", 0),
        feedback_signal_count=learning_signal.get("feedback_signal_count", 0),
        observation_count=learning_signal.get("observation_count", 0),
        reflection_count=learning_signal.get("reflection_count", 0),
        policy_patch_count=learning_signal.get("policy_patch_count", 0),
        policy_patch_feedback_count=learning_signal.get("policy_patch_feedback_count", 0),
        policy_patch_no_patch_count=learning_signal.get("policy_patch_no_patch_count", 0),
        consolidation_run_count=learning_signal.get("consolidation_run_count", 0),
        consolidation_applied_count=learning_signal.get("consolidation_applied_count", 0),
        lineage_coverage=learning_signal.get("lineage_coverage", {}),
        cold_start_state=learning_signal.get("cold_start_state"),
        freshness_window_seconds=learning_signal.get("freshness_window_seconds"),
        observation_status=learning_signal.get("observation_status"),
        policy_patch_status=learning_signal.get("policy_patch_status"),
        consolidation_status=learning_signal.get("consolidation_status"),
        repair_actions=[
            "Run the observation daemon or `mnemos observe run --incremental` and inspect zero-output reason.",
            "Route reflection/recap lessons through PolicyPatchStore or record explicit no-patch feedback.",
            "Apply a trusted cognitive consolidation and verify per-candidate coverage rows.",
        ],
    )


def _check_cognitive_learning(config) -> Dict[str, Any]:
    cache = _ACTIVE_HEALTH_READINESS_CACHE.get()
    failure_code = _active_readiness_failure(cache)
    if failure_code:
        return _record_active_readiness_failure(cache, failure_code)
    report = cache.get("report") if cache is not None else None
    if isinstance(report, dict):
        metrics = report.get("metrics", {})
        learning_signal = metrics.get("learning_signal", {}) if isinstance(metrics, dict) else {}
        if isinstance(learning_signal, dict):
            return _check_cognitive_learning_from_signal(learning_signal)

    from core.ops.cognitive_readiness import build_learning_signal_report

    return _check_cognitive_learning_from_signal(build_learning_signal_report(config))


def _check_cognitive_readiness(config) -> Dict[str, Any]:
    from core.ops.cognitive_readiness import build_cognitive_readiness_report

    cache = _ACTIVE_HEALTH_READINESS_CACHE.get()
    failure_code = _active_readiness_failure(cache)
    if failure_code:
        return _record_active_readiness_failure(cache, failure_code)
    report = cache.get("report") if cache is not None else None
    if not isinstance(report, dict):
        try:
            report = build_cognitive_readiness_report(
                config,
                strict=True,
                enforce_budget=True,
            )
        except (ReadinessQueryDeadlineExceeded, sqlite3.Error):
            failure_code = _active_readiness_failure(cache)
            if failure_code:
                return _record_active_readiness_failure(cache, failure_code)
            raise
        failure_code = _active_readiness_failure(cache)
        if failure_code:
            return _record_active_readiness_failure(cache, failure_code)
        if cache is not None:
            cache["report"] = report
    budget = report.get("budget", {})
    scorecard = report.get("scorecard", {})
    readiness = report.get("readiness", {})
    failures = list(budget.get("failures", []) or [])
    repair_actions = sorted(
        {str(failure.get("repair_action")) for failure in failures if failure.get("repair_action")}
    )
    budget_ok = bool(budget.get("ok"))
    ok = bool(report.get("ok")) and budget_ok
    status = "ok" if ok else "degraded"
    details = {
        "schema_version": report.get("schema_version"),
        "ok": ok,
        "budget_ok": budget_ok,
        "failure_count": int(budget.get("failure_count", 0) or 0),
        "score": scorecard.get("score"),
        "max_score": scorecard.get("max_score"),
        "blocking_findings": scorecard.get("blocking_findings", []),
        "readiness_statuses": {
            name: section.get("status")
            for name, section in readiness.items()
            if isinstance(section, dict)
        },
        "failures": failures,
        "repair_actions": repair_actions,
        "mode": report.get("mode", {}),
        "generated_at": report.get("generated_at"),
        "lineage_coverage": report.get("metrics", {})
        .get("learning_signal", {})
        .get("lineage_coverage", {}),
        "cold_start_state": report.get("metrics", {})
        .get("learning_signal", {})
        .get("cold_start_state"),
    }
    if status != "ok":
        details["error"] = "cognitive readiness budget failed"
    return _item(status, **details)


def _check_security(config) -> Dict[str, Any]:
    from scripts.health_check import check_security

    return _check_result(check_security(config=config))


def _check_multimodal_model(config, *, show_sensitive: bool = False) -> Dict[str, Any]:
    from core.llm_config import resolve_multimodal_api_config

    summary = summarize_model_api_config(
        resolve_multimodal_api_config(config),
        "multimodal",
        show_sensitive=show_sensitive,
    )
    endpoint_status = str(summary.pop("status", "not_configured") or "not_configured")
    if endpoint_status == "configured":
        return _item(
            "ok",
            schema_version="mnemos.multimodal_optional.v1",
            optional=True,
            endpoint_status="configured",
            **summary,
        )
    if endpoint_status == "incomplete":
        return _item(
            "warning",
            schema_version="mnemos.multimodal_optional.v1",
            optional=True,
            endpoint_status="incomplete",
            error="multimodal endpoint is enabled but missing API key, model, or base_url",
            repair_actions=[
                "Set MNEMOS_MULTIMODAL_API_KEY, MNEMOS_MULTIMODAL_BASE_URL, and MNEMOS_MULTIMODAL_MODEL.",
                "Or set multimodal.enabled=false to keep image ingestion in recoverable manual mode.",
            ],
            **summary,
        )
    return _item(
        "skipped",
        schema_version="mnemos.multimodal_optional.v1",
        optional=True,
        endpoint_status="skipped",
        note=(
            "Optional multimodal model is not configured; unconfigured image inbox "
            "files create recoverable tasks."
        ),
        repair_actions=[
            "Set MNEMOS_MULTIMODAL_API_KEY, MNEMOS_MULTIMODAL_BASE_URL, and MNEMOS_MULTIMODAL_MODEL.",
        ],
        **summary,
    )


def _check_heartbeat(config) -> Dict[str, Any]:
    """Build the public daemon-heartbeat projection through the stable seam."""
    return _build_heartbeat_report(
        config,
        item=_item,
        verify_identity=_verify_heartbeat_identity,
        expected_service_ids=_expected_heartbeat_service_ids,
        project_services=_public_heartbeat_services,
        service_error_active=_heartbeat_service_error_active,
    )


def _status_rank(status: Any) -> int:
    return STATUS_RANK.get(str(status or "ok"), 1)


def _health_problem_text(name: str, check: Dict[str, Any]) -> str:
    detail = check.get("error")
    if not detail:
        warnings = check.get("warnings")
        if isinstance(warnings, list) and warnings:
            detail = "; ".join(str(item) for item in warnings[:2])
    return f"{name}: {detail or check.get('status', 'unknown')}"


def _is_optional_skipped(check: Dict[str, Any]) -> bool:
    return bool(check.get("optional")) and str(check.get("status")) == "skipped"


def _summarize_health(checks: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    strict_checks = [name for name in STRICT_HEALTH_CHECKS if name in checks]
    strict_failures = [
        name
        for name in strict_checks
        if _status_rank(checks[name].get("status")) >= STATUS_RANK["degraded"]
        or checks[name].get("status") == "skipped"
    ]
    failed_checks = [
        name
        for name, check in checks.items()
        if _status_rank(check.get("status")) >= STATUS_RANK["failed"]
    ]
    degraded_checks = [
        name
        for name, check in checks.items()
        if _status_rank(check.get("status")) == STATUS_RANK["degraded"]
    ]
    warning_checks = [
        name
        for name, check in checks.items()
        if not _is_optional_skipped(check)
        and (
            _status_rank(check.get("status")) == STATUS_RANK["warning"]
            or (
                name not in strict_checks
                and _status_rank(check.get("status")) >= STATUS_RANK["degraded"]
            )
        )
    ]
    if failed_checks:
        status = "failed"
    elif strict_failures:
        status = "degraded"
    elif warning_checks:
        status = "warning"
    else:
        status = "ok"
    errors = [
        _health_problem_text(name, checks[name])
        for name in failed_checks + strict_failures
        if checks[name].get("error") or checks[name].get("status") != "ok"
    ]
    warnings = [
        _health_problem_text(name, checks[name])
        for name in warning_checks
        if name not in strict_failures
    ]
    return {
        "status": status,
        "ok": status == "ok",
        "usable": status in {"ok", "warning"},
        "strict_ok": not strict_failures and not failed_checks,
        "strict_checks": strict_checks,
        "strict_failures": sorted(set(strict_failures)),
        "failed_checks": sorted(set(failed_checks)),
        "degraded_checks": sorted(set(degraded_checks)),
        "warning_checks": sorted(set(warning_checks)),
        "warnings": sorted(set(warnings)),
        "errors": sorted(set(errors)),
    }


def build_health_report(
    config: Any | None = None, *, show_sensitive: bool = False
) -> Dict[str, Any]:
    """Build a JSON-safe, read-only health report."""
    if config is None:
        from core.config import Config

        config = Config(provision=False)
    from core.ops.config_scope import use_config

    with use_config(config):
        return _build_health_report_with_config(
            config,
            show_sensitive=show_sensitive,
        )


def _build_health_report_with_config(
    config: Any, *, show_sensitive: bool
) -> Dict[str, Any]:
    """Run isolated checks against one immutable effective config."""

    # Cognitive readiness is expensive against a lossless Raw store.  The
    # learning subcheck is a projection of that same report, so health obtains
    # it once under its bounded read-only SQLite scope.
    with _health_cognitive_readiness_scope():
        cognitive_readiness_check = _safe_check(
            "cognitive_readiness", lambda: _check_cognitive_readiness(config)
        )
        cognitive_learning_check = _safe_check(
            "cognitive_learning", lambda: _check_cognitive_learning(config)
        )

    checks = {
        "storage": _safe_check("storage", lambda: _check_storage(config)),
        "wiki": _safe_check("wiki", lambda: _check_wiki(config)),
        "agent": _safe_check("agent", _check_agents),
        "daemon": _safe_check("daemon", _check_daemon),
        "event_bus": _safe_check("event_bus", lambda: _check_event_bus(config)),
        "schema": _safe_check("schema", lambda: _check_schema(config)),
        "amphora": _safe_check(
            "amphora",
            lambda: _call_optional_config(_check_amphora, config),
        ),
        "queues": _safe_check("queues", lambda: _check_queue_backlog(config)),
        "disk": _safe_check("disk", lambda: _check_disk(config)),
        "api": _safe_check(
            "api",
            lambda: _call_config_check(
                _check_api,
                config,
                show_sensitive=show_sensitive,
            ),
        ),
        "multimodal": _safe_check(
            "multimodal",
            lambda: _call_config_check(
                _check_multimodal_model,
                config,
                show_sensitive=show_sensitive,
            ),
        ),
        "heartbeat": _safe_check("heartbeat", lambda: _check_heartbeat(config)),
        "wiki_route": _safe_check("wiki_route", lambda: _check_wiki_route(config)),
        "wiki_projection": _safe_check(
            "wiki_projection", lambda: _check_wiki_projection(config)
        ),
        "system_contracts": _safe_check("system_contracts", _check_system_contracts),
        "module_toggles": _safe_check("module_toggles", lambda: _check_module_toggles(config)),
        "runtime_producer_consumer": _safe_check(
            "runtime_producer_consumer",
            lambda: _check_runtime_producer_consumer(config),
        ),
        "migrations": _safe_check("migrations", lambda: _check_migrations(config)),
        "backup": _safe_check("backup", lambda: _check_backup(config)),
        "data_ownership": _safe_check("data_ownership", lambda: _check_data_ownership(config)),
        "model_call_ledger": _safe_check(
            "model_call_ledger", lambda: _check_model_call_ledger(config)
        ),
        "golden_benchmark": _safe_check("golden_benchmark", _check_golden_benchmark),
        "distill_json_quality": _safe_check(
            "distill_json_quality", lambda: _check_distill_json_quality(config)
        ),
        "distill_cognitive_actions": _safe_check(
            "distill_cognitive_actions", lambda: _check_distill_cognitive_actions(config)
        ),
        "install_lifecycle": _safe_check(
            "install_lifecycle", lambda: _check_install_lifecycle(config)
        ),
        "sqlite_disk_budget": _safe_check(
            "sqlite_disk_budget", lambda: _check_sqlite_disk_budget(config)
        ),
        "adaptive_policy": _safe_check("adaptive_policy", lambda: _check_adaptive_policy(config)),
        "cognitive_readiness": cognitive_readiness_check,
        "cognitive_learning": cognitive_learning_check,
        "security": _safe_check("security", lambda: _check_security(config)),
    }
    from core.ops.auto_healing import (
        annotate_checks_with_auto_heal,
        build_health_auto_heal_report,
    )

    auto_healing = _safe_check(
        "auto_healing",
        lambda: build_health_auto_heal_report(config, checks),
    )
    annotate_checks_with_auto_heal(checks, auto_healing)
    checks["auto_healing"] = auto_healing
    actual_check_ids = tuple(checks)
    if actual_check_ids != CANONICAL_HEALTH_CHECK_IDS:
        raise RuntimeError(
            "health check contract drift: "
            f"expected={CANONICAL_HEALTH_CHECK_IDS!r} actual={actual_check_ids!r}"
        )
    summary = _summarize_health(checks)
    report = {
        "ok": summary["ok"],
        "usable": summary["usable"],
        "strict_ok": summary["strict_ok"],
        "status": summary["status"],
        "checks": checks,
        "strict_checks": summary["strict_checks"],
        "strict_failures": summary["strict_failures"],
        "failed_checks": summary["failed_checks"],
        "degraded_checks": summary["degraded_checks"],
        "warning_checks": summary["warning_checks"],
        "warnings": summary["warnings"],
        "errors": summary["errors"],
        "health_check_ids": list(CANONICAL_HEALTH_CHECK_IDS),
        "health_check_ids_hash": CANONICAL_HEALTH_CHECK_IDS_HASH,
    }
    if show_sensitive:
        return report
    return cast(Dict[str, Any], redact_sensitive_data(report))


def build_health_report_quiet(
    config: Any | None = None, *, show_sensitive: bool = False
) -> Dict[str, Any]:
    """Build a report while suppressing incidental stdout/stderr from imports."""
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
        return build_health_report(config, show_sensitive=show_sensitive)


def print_human_report(report: Dict[str, Any]) -> None:
    print("Mnemos Health")
    print("=" * 40)
    print(f"status: {report['status']}")
    print(f"usable: {report.get('usable', report.get('ok'))}")
    print(f"strict_ok: {report.get('strict_ok', report.get('ok'))}")
    if report.get("strict_failures"):
        print("strict_failures: " + ", ".join(report["strict_failures"]))
    for name, check in report["checks"].items():
        print(f"{name}: {check['status']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mnemos machine-readable health check")
    parser.add_argument("--json", action="store_true", help="emit pure JSON")
    parser.add_argument(
        "--unsafe-debug",
        action="store_true",
        help="include local paths and endpoint hosts in output",
    )
    args = parser.parse_args(argv)

    if args.json:
        report = build_health_report_quiet(show_sensitive=bool(args.unsafe_debug))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        report = build_health_report(show_sensitive=bool(args.unsafe_debug))
        print_human_report(report)
    return 0 if report.get("usable", report["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
