#!/usr/bin/env python3
"""Inventory legacy belief-like objects and quarantine exact candidates only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.belief_migration import BeliefCandidateReconciler  # noqa: E402
from core.config import get_config  # noqa: E402
from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--wiki-root", type=Path, action="append")
    parser.add_argument("--cognitive-graph-db", type=Path, action="append")
    parser.add_argument("--reflection-db", type=Path, action="append")
    parser.add_argument("--profile-db", type=Path, action="append")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-inventory-hash")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = get_config()
    database_dir = Path(config.database_dir)
    state_db = (args.state_db or database_dir / "producer_consumer_ledger.db").expanduser()
    wiki_roots = tuple(args.wiki_root or (Path(config.wiki_dir),))
    graph_dbs = tuple(args.cognitive_graph_db or (Path(config.cognitive_graph_db_path),))
    reflection_dbs = tuple(args.reflection_db or (database_dir / "reflections.db",))
    profile_dbs = tuple(args.profile_db or (database_dir / "persona.db",))
    if args.apply and args.backup_dir is None:
        return _emit(
            {"ok": False, "error": "--apply requires --backup-dir"},
            compact=args.json,
            exit_code=2,
        )
    if args.apply and not str(args.expected_inventory_hash or "").strip():
        return _emit(
            {"ok": False, "error": "--apply requires --expected-inventory-hash"},
            compact=args.json,
            exit_code=2,
        )
    if args.apply and not runtime_writers_are_inactive(state_db.parent):
        return _emit(
            {"ok": False, "error": "daemon_not_inactive"},
            compact=args.json,
            exit_code=2,
        )
    try:
        report = BeliefCandidateReconciler(
            state_db=state_db,
            wiki_roots=wiki_roots,
            cognitive_graph_dbs=graph_dbs,
            reflection_dbs=reflection_dbs,
            profile_dbs=profile_dbs,
        ).reconcile(
            apply=args.apply,
            backup_dir=args.backup_dir,
            confirm_daemon_stopped=args.apply,
            expected_inventory_hash=args.expected_inventory_hash,
        )
        payload = {
            "ok": bool(
                not args.apply
                or (
                    report["state_integrity_check"] == "ok"
                    and report["active_head_delta"] == 0
                    and report["active_revision_delta"] == 0
                )
            ),
            "state_db": str(state_db),
            **report,
        }
    except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        payload = {
            "ok": False,
            "state_db": str(state_db),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return _emit(payload, compact=args.json, exit_code=0 if payload.get("ok") else 1)


def _emit(payload: dict[str, object], *, compact: bool, exit_code: int) -> int:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if compact else 2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
