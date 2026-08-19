"""Bounded PredictionLedger maturity service adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.prediction_ledger import PredictionRecordStore
from core.cognitive.state_store import CognitiveStateStore


def run_service(
    log_service_error: Callable[[str, Exception], None],
    *,
    config: Any | None = None,
    now: datetime | str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Close one bounded batch; absent outcome evidence is terminal, not retry."""

    from core.config import get_config

    cfg = config or get_config()
    if not bool(cfg.get("daemon.services.prediction_maturity", True)):
        return {
            "status": "skipped",
            "reason": "daemon_service_disabled",
            "selected": 0,
        }
    state_store = CognitiveStateStore(cfg)
    if not state_store.db_path.is_file():
        return {
            "status": "not_initialized",
            "reason": "cognitive_state_store_absent",
            "selected": 0,
        }
    batch_limit = int(
        limit
        if limit is not None
        else cfg.get("prediction.maturity_batch_limit", 100)
    )
    if batch_limit <= 0:
        raise ValueError("prediction maturity batch limit must be positive")
    try:
        projection = CognitiveStateApplicationService(
            state_store
        ).reconcile_outcome_projections(limit=batch_limit)
        if projection["failed"]:
            return {
                "status": "degraded",
                "reason": "outcome_projection_batch_has_failures",
                "selected": 0,
                "outcome_projection_selected": projection["selected"],
                "outcome_projection_committed": projection["committed"],
                "outcome_projection_failed": projection["failed"],
                "outcome_projection_remaining": projection["remaining"],
                "outcome_projection_failed_command_ids": projection[
                    "failed_command_ids"
                ],
            }
        receipt = PredictionRecordStore(state_store, config=cfg).reconcile_matured(
            now=now,
            limit=batch_limit,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        log_service_error("prediction_maturity", exc)
        return {
            "status": "error",
            "reason": type(exc).__name__,
            "selected": 0,
        }
    has_failures = receipt.failed > 0
    return {
        "status": "degraded" if has_failures else "ok",
        "reason": (
            "maturity_batch_has_failures"
            if has_failures
            else "maturity_batch_reconciled"
        ),
        "outcome_projection_selected": projection["selected"],
        "outcome_projection_committed": projection["committed"],
        "outcome_projection_failed": projection["failed"],
        "outcome_projection_remaining": projection["remaining"],
        "selected": receipt.selected,
        "measured": receipt.measured,
        "unknown": receipt.unknown,
        "censored": receipt.censored,
        "confounded": receipt.confounded,
        "existing": receipt.existing,
        "failed": receipt.failed,
        "retryable_failed": receipt.retryable_failed,
        "terminal_failed": receipt.terminal_failed,
        "remaining_mature_open": receipt.remaining_mature_open,
        "revision_ids": list(receipt.revision_ids),
        "failed_prediction_ids": list(receipt.failed_prediction_ids),
        "retryable_failed_prediction_ids": list(
            receipt.retryable_failed_prediction_ids
        ),
        "terminal_failed_prediction_ids": list(
            receipt.terminal_failed_prediction_ids
        ),
    }
