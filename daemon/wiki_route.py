"""Wiki route daemon service helper."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict


def run_service(log_error: Callable[[str, Exception], None] | None = None) -> Dict[str, Any]:
    """Route classifiable Inbox Wiki pages into formal Obsidian folders."""
    result: Dict[str, Any] = {"status": "ok", "classified": 0, "moved": 0, "review": 0}
    try:
        from core.kia.charon import run_connect_cycle

        cycle = run_connect_cycle(dry_run=False, write_relations=False)
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
        if log_error:
            log_error("wiki_route", exc)
        result.update(status="error", error=str(exc))
        return result

    result.update({key: cycle.get(key, 0) for key in ("classified", "moved", "review")})
    result["counts"] = cycle
    try:
        from core.config import get_config
        from core.wiki_projection_lifecycle import WikiProjectionLedger
        from core.wiki_projection_publisher import publish_unpublished_mutations

        cfg = get_config()
        ledger = WikiProjectionLedger()
        result["wiki_mutations"] = ledger.reconcile_vault(cfg.wiki_dir)
        result["wiki_events"] = publish_unpublished_mutations(ledger, limit=100)
        result["wiki_projection"] = ledger.reconciliation_report()
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
        LookupError,
    ) as exc:
        if log_error:
            log_error("wiki_route", exc)
        result.update(status="error", error=str(exc))
    return result
