#!/usr/bin/env python3
"""Inspect or apply exact COG-014 target-state provenance reconciliation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.hephaestus.cognitive_action_state_reconciliation import (  # noqa: E402
    CognitiveActionStateReconciliationPaths,
    apply_cognitive_action_state_reconciliation,
    build_cognitive_action_state_reconciliation_plan,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the dry-run/apply CLI contract for exact reviewed hashes."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-inventory-hash", default="")
    parser.add_argument("--expected-object-manifest-hash", default="")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a read-only preview or one backup-first reviewed apply."""

    args = build_parser().parse_args(argv)
    if args.database_dir is None:
        database_dir = Path(get_config().database_dir).expanduser()
    else:
        database_dir = args.database_dir.expanduser()
    paths = CognitiveActionStateReconciliationPaths(database_dir=database_dir)
    if not args.apply:
        try:
            plan = build_cognitive_action_state_reconciliation_plan(paths)
            payload = {**plan.as_dict(), "dry_run": True}
        except (OSError, RuntimeError, ValueError) as exc:
            payload = {
                "ok": False,
                "status": "blocked",
                "dry_run": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        _print(payload, compact=args.json)
        return 0 if payload.get("ok") else 1

    if args.backup_dir is None:
        payload = {
            "ok": False,
            "status": "blocked",
            "dry_run": False,
            "error": "--apply requires --backup-dir",
        }
        _print(payload, compact=args.json)
        return 2
    try:
        payload = apply_cognitive_action_state_reconciliation(
            paths,
            expected_inventory_hash=str(args.expected_inventory_hash),
            expected_object_manifest_hash=str(args.expected_object_manifest_hash),
            backup_dir=args.backup_dir.expanduser(),
        )
        payload = {**payload, "dry_run": False}
    except (OSError, RuntimeError, ValueError) as exc:
        payload = {
            "ok": False,
            "status": "blocked",
            "dry_run": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    _print(payload, compact=args.json)
    return 0 if payload.get("ok") else 1


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
