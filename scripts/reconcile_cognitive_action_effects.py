#!/usr/bin/env python3
"""Inspect, migrate, and optionally replay legacy cognitive-action receipts."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.hephaestus.cognitive_action_effect_audit import (  # noqa: E402
    audit_cognitive_action_effects,
)
from core.hephaestus.distill_action_reconciliation import (  # noqa: E402
    backup_database,
    inspect_reconciliation,
    migrate_historical_database,
)
from core.hephaestus.distill_cognitive_action_worker import (  # noqa: E402
    DistillCognitiveActionWorker,
)


TARGET_DB_NAMES = (
    "observations.db",
    "reflections.db",
    "policy_patches.db",
    "knowledge_graph.db",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--process", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = get_config()
    database_dir = (args.database_dir or Path(cfg.database_dir)).expanduser()
    db_path = (args.db_path or database_dir / "distill_actions.db").expanduser()
    wiki_dir = (args.wiki_dir or Path(cfg.wiki_dir)).expanduser()
    if (args.apply or args.process) and args.backup_dir is None:
        payload = {"ok": False, "error": "--apply/--process requires --backup-dir"}
        _print(payload, compact=args.json)
        return 2
    if args.process and not args.apply:
        payload = {"ok": False, "error": "--process requires --apply"}
        _print(payload, compact=args.json)
        return 2

    before = inspect_reconciliation(db_path)
    if not args.apply:
        payload = {
            "ok": before["schema_state"] in {"missing_database", "uninitialized", "current_v2"},
            "dry_run": True,
            "reconciliation": before,
            "effect_audit": audit_cognitive_action_effects(db_path),
        }
        _print(payload, compact=args.json)
        return 0 if payload["ok"] else 1

    backup_dir = Path(args.backup_dir).expanduser()
    target_backups = _backup_targets(database_dir, backup_dir)
    migration: dict[str, Any]
    try:
        migration = migrate_historical_database(
            db_path,
            database_dir=database_dir,
            backup_dir=backup_dir,
        )
        processing = None
        if args.process:
            processing = _summarize_processing(
                DistillCognitiveActionWorker(
                    db_path,
                    database_dir=database_dir,
                    wiki_dir=wiki_dir,
                    worker_id="cog014-reconciliation",
                ).process_queued(limit=max(0, int(args.limit)))
            )
        audit = audit_cognitive_action_effects(db_path)
        payload = {
            "ok": bool(audit["ok"]),
            "dry_run": False,
            "target_backups": target_backups,
            "migration": migration,
            "processing": processing,
            "effect_audit": audit,
        }
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        payload = {
            "ok": False,
            "dry_run": False,
            "target_backups": target_backups,
            "error": f"{type(exc).__name__}: {exc}",
        }
    _print(payload, compact=args.json)
    return 0 if payload.get("ok") else 1


def _backup_targets(database_dir: Path, backup_dir: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target_dir = backup_dir / f"target-databases-{stamp}"
    for name in TARGET_DB_NAMES:
        source = database_dir / name
        if source.is_file():
            result.append(backup_database(source, target_dir))
    return result


def _summarize_processing(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep reconciliation output count-only and free of artifact/error text."""
    items = report.get("items")
    safe_items = [item for item in items if isinstance(item, Mapping)] if isinstance(
        items, list
    ) else []
    status_counts = Counter(str(item.get("status") or "unknown") for item in safe_items)
    error_types = Counter(
        str(item.get("error") or "unknown").split(":", 1)[0]
        for item in safe_items
        if item.get("error")
    )
    return {
        key: value
        for key, value in report.items()
        if key != "items" and isinstance(value, (str, int, float, bool, type(None)))
    } | {
        "item_count": len(safe_items),
        "status_counts": dict(sorted(status_counts.items())),
        "error_types": dict(sorted(error_types.items())),
    }


def _print(payload: Mapping[str, Any], *, compact: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if compact else 2,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
