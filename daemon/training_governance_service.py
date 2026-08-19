"""Bounded daemon adapter for governed training projection and runs."""

from __future__ import annotations

from typing import Any, Callable

from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.training_governance import TrainingGovernanceStore
from core.cognitive.training_contract import TRAINING_DIMENSION


def run_service(
    log_service_error: Callable[[str, Exception], None],
    *,
    config: Any | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Reconcile one bounded projection page and one deterministic run."""

    from core.config import get_config

    cfg = config or get_config()
    if not bool(cfg.get("daemon.services.training_governance", True)):
        return {
            "status": "skipped",
            "reason": "daemon_service_disabled",
            "projected": 0,
        }
    state = CognitiveStateStore(cfg)
    scoring_db = state.db_path.parent / "mnemos.db"
    if not state.db_path.is_file() or not scoring_db.is_file():
        return {
            "status": "not_initialized",
            "reason": "training_governance_store_absent",
            "projected": 0,
        }
    batch_limit = int(
        limit if limit is not None else cfg.get("training_governance.reconcile_batch_limit", 100)
    )
    try:
        governance = TrainingGovernanceStore(
            state,
            database_dir=state.db_path.parent,
        )
        intake = governance.reconcile_admission_intakes(batch_limit)
        if intake.failed:
            return {
                "status": "degraded",
                "reason": "training_admission_intake_has_failures",
                "admission_committed": intake.committed,
                "admission_superseded": intake.superseded,
                "admission_deferred": intake.deferred,
                "admission_failed": intake.failed,
                "admission_remaining": intake.remaining,
                "projected": 0,
                "run_status": "deferred",
            }
        projection = governance.reconcile_pending(batch_limit)
        if projection.failed:
            return {
                "status": "degraded",
                "reason": "training_projection_has_failures",
                "projected": projection.projected,
                "failed": projection.failed,
                "remaining": projection.remaining,
                "run_status": "deferred",
            }
        stale = [
            revision
            for revision in state.current_revisions(object_type="training_run_record")
            if revision.payload["dimension"] == TRAINING_DIMENSION
            and revision.payload["state"] == "stale"
        ]
        if stale:
            run = governance.rebuild_stale_dimension(TRAINING_DIMENSION)
        else:
            run = governance.build_ready_run(TRAINING_DIMENSION)
            if run.status == "sealed":
                run = governance.apply_run(run.run_revision_id)
    except (OSError, RuntimeError, TypeError, ValueError, PermissionError) as exc:
        log_service_error("training_governance", exc)
        return {
            "status": "error",
            "reason": type(exc).__name__,
            "projected": 0,
        }
    return {
        "status": "ok",
        "reason": "training_governance_reconciled",
        "admission_committed": intake.committed,
        "admission_superseded": intake.superseded,
        "admission_deferred": intake.deferred,
        "admission_failed": intake.failed,
        "admission_remaining": intake.remaining,
        "projected": projection.projected,
        "failed": projection.failed,
        "remaining": projection.remaining,
        "run_status": run.status,
        "run_revision_id": run.run_revision_id,
        "model_id": run.model_id,
    }
