#!/usr/bin/env python3
"""Back up and initialize ROOT-010 recap consumption/correction schemas."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _table_exists(db_path: Path, table: str) -> bool:
    if not db_path.exists():
        return False
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        )


def _columns(db_path: Path, table: str) -> set[str]:
    if not db_path.exists() or not _table_exists(db_path, table):
        return set()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _count(db_path: Path, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    if not _table_exists(db_path, table):
        return 0
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return int(conn.execute(query, params).fetchone()[0])


def _historical_recap_pages(wiki_dir: Path) -> int:
    count = 0
    if not wiki_dir.exists():
        return 0
    for path in wiki_dir.rglob("*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:8192]
        except OSError:
            continue
        if "mnemos_type: retrospective" in head and "recap_id:" in head:
            count += 1
    return count


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(str(destination)) as dst:
            src.backup(dst)


def _integrity(db_path: Path) -> str:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def reconcile(
    *,
    database_dir: Path,
    wiki_dir: Path,
    backup_root: Path,
    apply: bool,
) -> dict[str, Any]:
    database_dir = Path(database_dir).expanduser()
    wiki_dir = Path(wiki_dir).expanduser()
    backup_root = Path(backup_root).expanduser()
    recap_db = database_dir / "recap_tasks.db"
    user_db = database_dir / "user_signals.db"
    policy_db = database_dir / "policy_patches.db"
    reminder_db = database_dir / "dialog_reminder.db"

    recap_schema_missing = "plan_id" not in _columns(
        recap_db, "recap_consumption_plans"
    )
    feedback_schema_missing = "schema_version" not in _columns(
        recap_db, "recap_feedback_events"
    )
    outcome_schema_missing = not _table_exists(
        recap_db, "recap_consumption_outcomes"
    ) or "source_event_id" not in _columns(recap_db, "recap_consumption_outcomes")
    scheduler_correction_schema_missing = not _table_exists(
        recap_db, "recap_scheduler_corrections"
    )
    # Both recap schemas are installed by one ledger initialization action.
    recap_change = (
        recap_schema_missing
        or feedback_schema_missing
        or outcome_schema_missing
        or scheduler_correction_schema_missing
    )
    signal_change = "source_event_id" not in _columns(user_db, "reflection_signals")
    policy_change = "source_event_id" not in _columns(
        policy_db, "policy_patch_feedback"
    )
    reminder_change = not _table_exists(reminder_db, "dialog_reminder_corrections")
    report: dict[str, Any] = {
        "schema_version": "mnemos.recap_consumption_reconciliation.v1",
        "apply": bool(apply),
        "ok": True,
        "database_dir": str(database_dir),
        "wiki_dir": str(wiki_dir),
        "schema_changes_required": (
            int(recap_change)
            + int(signal_change)
            + int(policy_change)
            + int(reminder_change)
        ),
        "historical_recap_pages": _historical_recap_pages(wiki_dir),
        "retrospective_policy_patches": _count(
            policy_db,
            "policy_patches",
            "source_type='retrospective'",
        ),
        "recap_tasks": _count(recap_db, "recap_tasks"),
        "existing_consumption_plans": _count(recap_db, "recap_consumption_plans"),
        "existing_feedback_events": _count(recap_db, "recap_feedback_events"),
        "legacy_consumption_plans": (
            _count(recap_db, "recap_consumption_plans") if recap_schema_missing else 0
        ),
        "historical_backfill_count": 0,
        "historical_unknown_count": 0,
        "backed_up": [],
        "backup_dir": "",
        "integrity": {},
    }
    # A page/patch can only be backfilled with a full exact source identity. This
    # initializer deliberately leaves any future non-zero candidates for the
    # explicit evidence reconciliation pass rather than inventing receipts.
    report["historical_unknown_count"] = (
        report["historical_recap_pages"]
        + report["retrospective_policy_patches"]
        + report["legacy_consumption_plans"]
    )
    if not apply:
        return report

    stamp = datetime.now().strftime("root010-recap-consumption-%Y%m%d-%H%M%S-%f")
    backup_dir = backup_root / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    for source in (recap_db, user_db, policy_db, reminder_db):
        if source.exists():
            _backup_sqlite(source, backup_dir / source.name)
            report["backed_up"].append(source.name)
    report["backup_dir"] = str(backup_dir)

    from core.app.recap_consumption import RecapConsumptionLedger
    from core.app.recap_feedback import RecapFeedbackOutbox
    from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
    from core.cognitive.policy_patch import PolicyPatchOptions, PolicyPatchStore
    from core.kia.dialog_reminder import DialogReminderQueue
    from core.persona.psyche import SignalStore

    RecapConsumptionLedger(recap_db)
    RecapFeedbackOutbox(recap_db)
    RetrospectiveConsumptionRouter(db_path=recap_db)
    signal_store = SignalStore(db_path=user_db)
    signal_store.close()
    PolicyPatchStore(
        options=PolicyPatchOptions(
            database_dir=database_dir,
            db_path=policy_db,
        )
    )
    DialogReminderQueue(db_path=str(reminder_db))

    report["integrity"] = {
        path.name: _integrity(path)
        for path in (recap_db, user_db, policy_db, reminder_db)
        if path.exists()
    }
    report["ok"] = all(value == "ok" for value in report["integrity"].values())
    report["schema_changes_required"] = 0
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up and apply schema initialization")
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit JSON (default output is also JSON)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    from core.config import get_config

    cfg = get_config()
    database_dir = args.database_dir or cfg.database_dir
    wiki_dir = args.wiki_dir or cfg.wiki_dir
    backup_root = args.backup_root or (Path(cfg.database_dir) / "backups")
    report = reconcile(
        database_dir=Path(database_dir),
        wiki_dir=Path(wiki_dir),
        backup_root=Path(backup_root),
        apply=bool(args.apply),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
