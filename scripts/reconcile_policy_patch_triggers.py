#!/usr/bin/env python3
"""Audit and safely reconcile legacy PolicyPatch trigger terms."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.policy_patch import PolicyPatchOptions, PolicyPatchStore  # noqa: E402
from core.document_import import file_sha256  # noqa: E402


def _backup_database(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / source.name
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {destination}")
    with sqlite3.connect(str(source), timeout=30) as source_conn, sqlite3.connect(
        str(destination), timeout=30
    ) as destination_conn:
        source_conn.backup(destination_conn)
    return destination


def reconcile(*, apply: bool, backup_dir: Path | None = None) -> dict:
    options = PolicyPatchOptions.from_config()
    store = PolicyPatchStore(options=options, ensure_db=False)
    result = store.reconcile_trigger_terms(apply=False)
    result["backup"] = ""
    if not apply or not result["changed"]:
        return result
    if not options.db_path.is_file():
        raise FileNotFoundError(options.db_path)
    if backup_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = options.database_dir / "backups" / "policy-patch-triggers" / stamp
    backup_path = _backup_database(options.db_path, backup_dir)
    result["backup"] = str(backup_path)
    created_at = datetime.now().astimezone().isoformat()
    material_action = store.prepare_reconcile_material_action(
        list(result["changes"]),
        source_facts={
            "database_path": str(options.db_path.resolve(strict=True)),
            "backup_path": str(backup_path.resolve(strict=True)),
            "backup_sha256": f"sha256:{file_sha256(backup_path)}",
            "preview": dict(result),
        },
        evidence_refs=(
            f"policy-patch-database:{options.db_path.resolve(strict=True)}",
            f"policy-patch-backup:sha256:{file_sha256(backup_path)}",
        ),
        created_at=created_at,
        producer="reconcile-policy-patch-triggers",
    )
    applied = store.reconcile_trigger_terms(
        apply=True,
        material_action=material_action,
    )
    applied["backup"] = result["backup"]
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = reconcile(
        apply=args.apply,
        backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "PolicyPatch trigger reconciliation: "
            f"applied={result['applied']} changed={result['changed']} "
            f"moved_to_review={result['moved_to_review']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
