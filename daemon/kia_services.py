# -*- coding: utf-8 -*-
"""KIA-oriented daemon service helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def run_recap_consumption(
    log_service_error: Callable[[str, Exception], None],
    *,
    limit: int = 100,
) -> Dict[str, Any]:
    """Drain durable recap consumption and correction receipts."""
    result: Dict[str, Any] = {
        "enabled": True,
        "processed": 0,
        "plans_processed": 0,
        "feedback_events_processed": 0,
        "errors": 0,
    }
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("daemon.services.recap_consumption", True):
            result["enabled"] = False
            return result
        from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter

        drained = RetrospectiveConsumptionRouter(
            db_path=cfg.database_dir / "recap_tasks.db"
        ).drain_pending(limit=limit)
        result["plans_processed"] = int(drained.get("plans_processed", 0) or 0)
        result["feedback_events_processed"] = int(drained.get("feedback_events_processed", 0) or 0)
        result["processed"] = result["plans_processed"] + result["feedback_events_processed"]
        result["errors"] = int(drained.get("errors", 0) or 0)
        if result["errors"]:
            result["status"] = "retryable_failed"
            log_service_error(
                "recap_consumption",
                RuntimeError(
                    f"{result['errors']} recap consumption/correction item(s) remain incomplete"
                ),
            )
        else:
            result["status"] = "processed" if result["processed"] else "up_to_date"
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
    ) as exc:
        log_service_error("recap_consumption", exc)
        result["errors"] += 1
    return result


def run_cognitive_graph_reconcile(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Consume cognitive graph outbox records and update missing relations."""
    result = {"enabled": True, "processed": 0, "relations": 0, "errors": 0}
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.cognitive_graph_enabled:
            result["enabled"] = False
            return result
        if not cfg.get("daemon.services.cognitive_graph_reconcile", True):
            result["enabled"] = False
            return result

        from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater

        store = CognitiveGraphStore()
        updater = CognitiveGraphUpdater(store=store)
        reconcile_result = updater.reconcile()
        result["processed"] = reconcile_result.get("outbox", {}).get("processed", 0)
        result["relations"] = reconcile_result.get("stats", {}).get("relations", 0)
        result["belief_projections"] = 0
        result["belief_projection_pending"] = 0
        state_path = Path(cfg.database_dir) / "producer_consumer_ledger.db"
        if state_path.is_file():
            from core.cognitive.belief_revision import BeliefRevisionProjector
            from core.cognitive.state_store import CognitiveStateStore

            belief_result = BeliefRevisionProjector(
                CognitiveStateStore(state_path),
                store,
            ).process_pending(limit=1000)
            result["belief_projections"] = int(belief_result.get("committed", 0))
            result["belief_projection_pending"] = int(
                belief_result.get("pending", 0)
            )
            belief_failures = int(belief_result.get("failed", 0))
            if belief_failures:
                result["errors"] += belief_failures
                log_service_error(
                    "cognitive_graph_reconcile",
                    RuntimeError(
                        f"{belief_failures} belief projection command(s) remain incomplete"
                    ),
                )
        if result["processed"] and log_info is not None:
            log_info(
                "[DAEMON] CognitiveGraph reconciliation: %d outbox processed, %d relations",
                result["processed"],
                result["relations"],
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("cognitive_graph_reconcile", exc)
        result["errors"] += 1
    return result


def _enqueue_reminders(
    high: List[Any],
    reminders: List[Any],
    result: Dict[str, Any],
    cfg: Any,
    log_info: Callable[..., None] | None,
    log_debug: Callable[..., None] | None,
) -> None:
    """把高优先级 freshness 提醒加入对话提醒队列。"""
    from core.kia.dialog_reminder import DialogReminderQueue

    if high:
        paths = [r.page_path for r in high[:10]]
        if log_info is not None:
            log_info(
                "[DAEMON] Reminder scan: %d stale pages, %d high priority: %s",
                len(reminders),
                len(high),
                paths,
            )
        queue = DialogReminderQueue()
        for reminder in high:
            try:
                reminder_id = queue.enqueue(
                    issue_id=f"freshness:{reminder.page_path}",
                    page_path=reminder.page_path,
                    severity="high",
                    content=(
                        f"📋 [[{reminder.title or Path(reminder.page_path).stem}]] "
                        f"可能已过期\n\n{reminder.message}"
                    ),
                    choices=["已更新", "仍有效", "稍后处理"],
                )
                from core.ops.runtime_flow_telemetry import record_runtime_produced

                record_runtime_produced(
                    "reminder_to_dialog_nudge",
                    source="core/kia/reminder_engine.py",
                    item_id=str(reminder_id),
                    intended_consumers=["core/kia/dialog_reminder.py"],
                    metadata={"transition": "dialog_reminder_enqueued"},
                    config_or_path=cfg,
                )
                result["enqueued"] += 1
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                if log_debug is not None:
                    log_debug(
                        "[DAEMON] 提醒入队失败 %s",
                        reminder.page_path,
                        exc_info=True,
                    )
    elif reminders and log_info is not None:
        log_info("[DAEMON] Reminder scan: %d stale pages", len(reminders))


def run_reminder_scan(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    log_debug: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Scan wiki freshness and enqueue high-priority stale page reminders."""
    result = {"stale_pages": 0, "high_priority": 0, "enqueued": 0, "errors": 0}
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("reminder.enabled", True):
            return result
        if not cfg.get("daemon.services.reminder_scan", True):
            return result

        from core.kia.reminder_engine import ReminderEngine

        engine = ReminderEngine()
        reminders = engine.scan_all_freshness()
        result["stale_pages"] = len(reminders)
        high = [r for r in reminders if r.priority == "high"]
        result["high_priority"] = len(high)

        _enqueue_reminders(high, reminders, result, cfg, log_info, log_debug)
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("reminder_scan", exc)
        result["errors"] += 1
    return result


def _refresh_high_priority_pages(
    worker: Any,
    high: List[Any],
    limit: int,
    result: Dict[str, Any],
    cfg: Any,
    log_debug: Callable[..., None] | None,
) -> None:
    """自动刷新高优先级过期页面。"""
    for reminder in high[:limit]:
        try:
            from core.ops.runtime_flow_telemetry import (
                record_runtime_consumed,
                record_runtime_produced,
                runtime_item_id,
            )

            freshness_item_id = runtime_item_id("wiki-page", reminder.page_path)
            record_runtime_produced(
                "freshness_to_search_and_refresh",
                source="core/kia/proteus.py",
                item_id=freshness_item_id,
                intended_consumers=["core/app/freshness_refresh_worker.py"],
                metadata={"transition": "high_priority_refresh_dispatched"},
                config_or_path=cfg,
            )
            refresh_result = worker.refresh_page(reminder.page_path)
            record_runtime_consumed(
                "freshness_to_search_and_refresh",
                source="core/app/freshness_refresh_worker.py",
                item_id=freshness_item_id,
                metadata={
                    "transition": "refresh_terminal",
                    "status": refresh_result.status,
                },
                config_or_path=cfg,
            )
            if refresh_result.status == "refreshed":
                result["refreshed"] += 1
            elif refresh_result.status == "skipped":
                result["skipped"] += 1
            else:
                result["errors"] += 1
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            result["errors"] += 1
            if log_debug is not None:
                log_debug("[DAEMON] 自动刷新失败 %s", reminder.page_path, exc_info=True)


def _archive_cold_pages(
    worker: Any,
    cfg: Any,
    result: Dict[str, Any],
    log_debug: Callable[..., None] | None,
) -> None:
    """归档冷知识页面。"""
    try:
        archive_limit = int(cfg.get("freshness_refresh.archive_limit", 10) or 10)
        archive_result = worker.archive_cold_pages(limit=archive_limit)
        result["archived"] = archive_result.get("archived", 0)
        result["archive_errors"] = archive_result.get("errors", 0)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        result["archive_errors"] += 1
        if log_debug is not None:
            log_debug("[DAEMON] 冷知识归档失败", exc_info=True)


def run_freshness_refresh(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    log_debug: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Refresh high-priority stale pages and archive cold knowledge."""
    result = {"refreshed": 0, "skipped": 0, "errors": 0, "archived": 0, "archive_errors": 0}
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("reminder.enabled", True):
            return result
        if not cfg.get("daemon.services.freshness_refresh", True):
            return result

        from core.app.freshness_refresh_worker import FreshnessRefreshWorker
        from core.kia.reminder_engine import ReminderEngine

        engine = ReminderEngine()
        reminders = engine.scan_all_freshness()
        high = [r for r in reminders if r.priority == "high"]
        limit = int(cfg.get("freshness_refresh.auto_refresh_limit", 3))
        worker = FreshnessRefreshWorker(wiki_base=str(cfg.wiki_dir))
        _refresh_high_priority_pages(worker, high, limit, result, cfg, log_debug)
        _archive_cold_pages(worker, cfg, result, log_debug)

        if (result["refreshed"] or result["archived"]) and log_info is not None:
            log_info(
                "[DAEMON] Freshness refresh: %d refreshed, %d archived",
                result["refreshed"],
                result["archived"],
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("freshness_refresh", exc)
        result["errors"] += 1
    return result


def _get_entropy_engine(
    cfg: Any,
    module_registry: Any | None,
    result: Dict[str, Any],
) -> tuple[Optional[Any], int]:
    """获取 EntropyEngine 实例并返回 (engine, sample_size)。

    如果 module_registry 中 eris 模块未运行，则更新 result 并返回 (None, 0)。
    """
    sample_size = int(cfg.get("entropy.scan_sample_size", 200))
    if module_registry is None:
        from core.kia.eris import EntropyEngine

        return EntropyEngine(), sample_size

    module_status = module_registry.start_module("eris")
    eris_status = module_status.get("eris", {})
    if eris_status.get("state") != "running":
        result["enabled"] = False
        if eris_status.get("state"):
            result["module_state"] = eris_status.get("state")
        if eris_status.get("reason"):
            result["module_reason"] = eris_status.get("reason")
        return None, sample_size

    engine = module_registry.get_instance("eris")
    if engine is None:
        raise RuntimeError("KIA module eris did not provide an instance")
    return engine, sample_size


def _enqueue_entropy_candidates(
    candidates: List[Any],
    result: Dict[str, Any],
    cfg: Any,
    log_debug: Callable[..., None] | None,
) -> None:
    """把熵减候选加入对话提醒队列。"""
    from core.kia.dialog_reminder import DialogReminderQueue

    queue = DialogReminderQueue()
    for candidate in candidates:
        try:
            strategy = candidate.merge_strategy
            if strategy not in ("delete_duplicate", "merge_into_one", "link_related"):
                continue
            severity = "high" if strategy in ("delete_duplicate", "merge_into_one") else "medium"
            issue_id = f"entropy:{candidate.page_a}:{candidate.page_b}"
            from core.ops.runtime_flow_telemetry import (
                record_runtime_consumed,
                record_runtime_produced,
                runtime_item_id,
            )

            entropy_item_id = runtime_item_id(
                "entropy-candidate", candidate.page_a, candidate.page_b, strategy
            )
            record_runtime_produced(
                "entropy_to_kg_cleanup",
                source="core/kia/eris.py",
                item_id=entropy_item_id,
                intended_consumers=["core/kia/dialog_reminder.py"],
                metadata={"transition": "entropy_candidate_selected", "strategy": strategy},
                config_or_path=cfg,
            )
            queue.enqueue(
                issue_id=issue_id,
                page_path=candidate.page_a,
                severity=severity,
                content=(
                    f"📋 熵减建议：{candidate.reason}\n\n"
                    f"策略：{strategy}\n推荐操作：{candidate.recommended_action}"
                ),
                choices=["查看详情", "忽略"],
            )
            record_runtime_consumed(
                "entropy_to_kg_cleanup",
                source="core/kia/dialog_reminder.py",
                item_id=entropy_item_id,
                metadata={"transition": "review_reminder_enqueued", "issue_id": issue_id},
                config_or_path=cfg,
            )
            result["enqueued"] += 1
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            if log_debug is not None:
                log_debug("[DAEMON] 熵减候选入队失败", exc_info=True)


def run_entropy_scan(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    log_debug: Callable[..., None] | None = None,
    module_registry: Any | None = None,
) -> Dict[str, Any]:
    """Scan merge candidates and enqueue entropy reduction reminders."""
    result = {"candidates": 0, "enqueued": 0, "errors": 0}
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("daemon.services.entropy_scan", True):
            return result

        engine, sample_size = _get_entropy_engine(cfg, module_registry, result)
        if engine is None:
            return result

        report = engine.scan(sample_size=sample_size)
        result["candidates"] = len(report.candidates)

        if not report.candidates:
            return result

        _enqueue_entropy_candidates(report.candidates, result, cfg, log_debug)

        if result["enqueued"] and log_info is not None:
            log_info(
                "[DAEMON] Entropy scan: %d candidates, %d enqueued",
                result["candidates"],
                result["enqueued"],
            )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("entropy_scan", exc)
        result["errors"] += 1
    return result


def run_dispute_scan(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Detect knowledge disputes and resolve or materialize dispute pages."""
    result = {"disputes_created": 0, "auto_resolved": 0, "merged": 0, "skipped": 0, "errors": 0}
    try:
        from core.app.dispute_resolver import DisputeResolver
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("dispute_scan.enabled", True):
            return result
        if not cfg.get("daemon.services.dispute_scan", True):
            return result

        resolver = DisputeResolver()
        report = resolver.scan()
        result.update(
            {
                key: report.get(key, 0)
                for key in ("disputes_created", "auto_resolved", "merged", "skipped")
            }
        )
        if any(result.get(key) for key in ("disputes_created", "auto_resolved", "merged")):
            if log_info is not None:
                log_info(
                    "[DAEMON] Dispute scan: %d created, %d auto-resolved, %d merged",
                    result["disputes_created"],
                    result["auto_resolved"],
                    result["merged"],
                )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("dispute_scan", exc)
        result["errors"] += 1
    return result
