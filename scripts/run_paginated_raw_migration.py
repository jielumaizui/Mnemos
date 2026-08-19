"""One-shot plan+apply for the paginated Raw vault migration.

Mirrors daemon.raw_projection_service flow (plan -> validate -> apply) in a
single process against a static snapshot, so the epoch/WAL guards cannot be
tripped by live capture writes. See scripts/project_raw_vault.py main().
"""

from __future__ import annotations

import argparse
from argparse import Namespace
import json
import logging
import os
import time
from pathlib import Path

from scripts import project_raw_vault as projection


def _default_mnemos_dir() -> Path:
    return Path(os.environ.get("MNEMOS_DIR", Path.home() / ".mnemos"))


def _parse_args() -> argparse.Namespace:
    mnemos_dir = _default_mnemos_dir()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-db",
        required=True,
        help="Path to the static raw_events snapshot DB to migrate against.",
    )
    parser.add_argument(
        "--canonical-db-identity",
        default=str(mnemos_dir / "raw_events.db"),
        help="Identity string of the canonical raw_events DB (default: %(default)s).",
    )
    parser.add_argument(
        "--backup-dir",
        default=str(mnemos_dir / "backups" / "raw-vault-projection-metadata"),
        help="Backup directory for projection metadata (default: %(default)s).",
    )
    parser.add_argument(
        "--raw-dir",
        default=str(Path.home() / "Documents" / "raw"),
        help="Raw vault directory (default: %(default)s).",
    )
    parser.add_argument(
        "--recover-plan-hash",
        default="",
        help="Plan hash of an interrupted run to recover; empty when none remains.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    cli = _parse_args()

    recovery = projection.recover_interrupted_projection(
        Path(cli.raw_dir),
        expected_plan_hash=cli.recover_plan_hash,
        expected_backup_dir=Path(cli.backup_dir),
    )
    print(f"RECOVERY {json.dumps(recovery, ensure_ascii=False, default=str)}", flush=True)
    args = Namespace(
        raw_dir="",
        db_path=cli.snapshot_db,
        canonical_db_identity=cli.canonical_db_identity,
        backup_dir=cli.backup_dir,
        max_files=0,
        chunk_turns=5,
        max_turn_chars=0,
        max_file_bytes=2097152,
        include_eligible_delete=False,
        expected_plan_hash="",
    )
    t0 = time.time()
    store, chunks, stats = projection.plan_projection(args)
    try:
        plan = projection.validate_projection_plan(stats.get("projection_plan"))
        args.expected_plan_hash = str(plan["plan_hash"])
        print(
            f"PLAN_OK plan_hash={args.expected_plan_hash} "
            f"changed={len(plan.get('changed_paths', []))} "
            f"stale={len(plan.get('stale_paths', []))} "
            f"desired={len(plan.get('desired_file_hashes', {}))} "
            f"elapsed={time.time() - t0:.0f}s",
            flush=True,
        )
        applied = projection.apply_projection(args, store, chunks, stats)
        print(
            json.dumps(
                {
                    "plan_hash": args.expected_plan_hash,
                    "elapsed_seconds": round(time.time() - t0, 1),
                    "applied": applied,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
