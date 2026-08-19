"""Daemon-owned, read-only Cognitive consolidation planning tick."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def run_service(
    log_service_error: Callable[[str, Exception], None],
    *,
    config: Any | None = None,
) -> dict[str, Any]:
    """Plan safely and retry only explicitly bound trusted-page receipts."""

    from core.config import get_config
    from core.cognitive.consolidator import CognitiveConsolidationOptions, CognitiveConsolidator

    cfg = config or get_config()
    if not bool(cfg.get("daemon.services.cognitive_consolidation", True)):
        return {"status": "skipped", "reason": "daemon_service_disabled", "planned": 0}
    options = CognitiveConsolidationOptions.from_config(cfg)
    raw_path = Path(cfg.get("raw_event_store.db_path", options.database_dir / "raw_events.db"))
    if not raw_path.is_file():
        return {"status": "not_initialized", "reason": "raw_events_db_absent", "planned": 0}
    try:
        consolidator = CognitiveConsolidator(options=options, config=cfg)
        try:
            report = consolidator.plan(apply=False, candidate_limit=options.candidate_limit)
            reconciliations = consolidator.reconcile_bound_runs(limit=options.candidate_limit)
        finally:
            consolidator.close()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        log_service_error("cognitive_consolidation", exc)
        return {"status": "error", "reason": type(exc).__name__, "planned": 0}
    return {
        "status": "planned",
        "reason": "read_only_plan_requires_trusted_commit",
        "planned": int(report["raw"]["candidate_count"]),
        "candidate_dispositions": list(report["coverage"]["candidate_dispositions"]),
        "raw_purge_allowed": False,
        "coverage_written": 0,
        "reconciliations": reconciliations,
    }
