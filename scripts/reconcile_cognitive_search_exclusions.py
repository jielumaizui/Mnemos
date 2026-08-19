#!/usr/bin/env python3
"""Reconcile exact legacy ACL gaps into an append-only search exclusion ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.search_exclusion_ledger import (
    SearchExclusionCandidate,
    initialize_search_exclusion_ledger,
    insert_search_exclusion,
    inventory_search_exclusions,
    iter_search_exclusion_candidates,
    load_search_exclusion_keys,
    search_exclusion_coverage,
    search_exclusion_identity_key,
    validate_search_exclusion_ledger,
)
from core.config import get_config
from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _candidate_factory(
    *,
    targets: Sequence[str],
    wiki_dir: Path,
    cognitive_graph_db: Path,
    evidence_graph_db: Path,
) -> Callable[[], Iterable[SearchExclusionCandidate]]:
    return lambda: iter_search_exclusion_candidates(
        targets=targets,
        wiki_dir=wiki_dir,
        cognitive_graph_db=cognitive_graph_db,
        evidence_graph_db=evidence_graph_db,
    )


def _connection_keys(
    connection: sqlite3.Connection,
) -> set[bytes]:
    return {
        search_exclusion_identity_key(tuple(str(value) for value in row))
        for row in connection.execute("""
            SELECT channel, source_locator_hash, source_table,
                   source_key_hash, source_row_hash
            FROM cognitive_search_exclusions
            """).fetchall()
    }


def _backup_sqlite(source: Path, destination: Path) -> dict[str, str]:
    if destination.exists():
        raise FileExistsError(f"backup already exists: {destination}")
    source_connection = sqlite3.connect(source)
    backup_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()
    with sqlite3.connect(destination) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError("cognitive search exclusion backup integrity failed")
    return {
        "path": str(destination),
        "source_sha256": _sha256_file(source),
        "backup_sha256": _sha256_file(destination),
        "integrity_check": integrity,
    }


def _remove_sqlite_target(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)


def _restore_sqlite_backup(backup: Path, target: Path) -> None:
    """Restore one reviewed SQLite backup after a post-commit failure."""

    _remove_sqlite_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(backup)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    with sqlite3.connect(target) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError("restored cognitive search exclusion ledger is invalid")


def _write_review_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def reconcile(
    *,
    targets: Sequence[str],
    wiki_dir: Path,
    cognitive_graph_db: Path,
    evidence_graph_db: Path,
    exclusion_db: Path,
    apply: bool = False,
    backup_dir: Path | None = None,
    expected_inventory_hash: str = "",
    expected_object_manifest_hash: str = "",
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
    failpoint: str = "",
) -> dict[str, Any]:
    if not apply:
        return _reconcile_unlocked(
            targets=targets,
            wiki_dir=wiki_dir,
            cognitive_graph_db=cognitive_graph_db,
            evidence_graph_db=evidence_graph_db,
            exclusion_db=exclusion_db,
            apply=False,
            backup_dir=backup_dir,
            expected_inventory_hash=expected_inventory_hash,
            expected_object_manifest_hash=expected_object_manifest_hash,
            failpoint=failpoint,
        )
    with offline_migration_lock(exclusion_db.parent, daemon_check=daemon_check):
        return _reconcile_unlocked(
            targets=targets,
            wiki_dir=wiki_dir,
            cognitive_graph_db=cognitive_graph_db,
            evidence_graph_db=evidence_graph_db,
            exclusion_db=exclusion_db,
            apply=True,
            backup_dir=backup_dir,
            expected_inventory_hash=expected_inventory_hash,
            expected_object_manifest_hash=expected_object_manifest_hash,
            failpoint=failpoint,
        )


def _reconcile_unlocked(
    *,
    targets: Sequence[str],
    wiki_dir: Path,
    cognitive_graph_db: Path,
    evidence_graph_db: Path,
    exclusion_db: Path,
    apply: bool,
    backup_dir: Path | None,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
    failpoint: str,
) -> dict[str, Any]:
    factory = _candidate_factory(
        targets=targets,
        wiki_dir=wiki_dir,
        cognitive_graph_db=cognitive_graph_db,
        evidence_graph_db=evidence_graph_db,
    )
    inventory = inventory_search_exclusions(factory())
    keys, ledger_validation = load_search_exclusion_keys(exclusion_db)
    coverage = search_exclusion_coverage(factory(), exclusion_keys=keys)
    report: dict[str, Any] = {
        **inventory,
        "mode": "apply" if apply else "dry_run",
        "targets": list(targets),
        "exclusion_db": str(exclusion_db),
        "ledger_validation": ledger_validation,
        "covered_count": coverage["covered_count"],
        "uncovered_count": coverage["uncovered_count"],
        "would_insert_count": coverage["uncovered_count"],
        "inserted_count": 0,
        "existing_count": 0,
        "backup": {},
    }
    if not apply:
        return report
    if backup_dir is None:
        raise ValueError("apply requires an explicit backup directory")
    if not expected_inventory_hash or not expected_object_manifest_hash:
        raise ValueError("apply requires reviewed inventory and object manifest hashes")
    if expected_inventory_hash != inventory["inventory_hash"]:
        raise ValueError("inventory changed since review")
    if expected_object_manifest_hash != inventory["object_manifest_hash"]:
        raise ValueError("object manifest changed since review")
    if exclusion_db.exists() and not ledger_validation.get("ok"):
        raise RuntimeError("existing cognitive search exclusion ledger is invalid")
    if backup_dir.exists() and (not backup_dir.is_dir() or any(backup_dir.iterdir())):
        raise ValueError("backup directory must not exist or must be empty")
    resolved_backup = backup_dir.expanduser().resolve(strict=False)
    protected_paths = (
        wiki_dir.expanduser().resolve(strict=False),
        cognitive_graph_db.expanduser().resolve(strict=False),
        evidence_graph_db.expanduser().resolve(strict=False),
        exclusion_db.expanduser().resolve(strict=False),
    )
    for protected in protected_paths:
        if resolved_backup == protected or resolved_backup.is_relative_to(protected):
            raise ValueError("backup directory must be disjoint from every source and target")
    backup_dir.mkdir(parents=True, exist_ok=True)
    review_manifest = backup_dir / "reviewed-cognitive-search-exclusions.json"
    manifest: dict[str, Any] = {
        "schema_version": "mnemos.cognitive_search_exclusion_reconciliation.v1",
        "status": "prepared",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "candidate_count": inventory["candidate_count"],
        "channel_counts": inventory["channel_counts"],
        "table_counts": inventory["table_counts"],
        "targets": list(targets),
        "target_existed": exclusion_db.is_file(),
    }
    _write_review_manifest(review_manifest, manifest)

    target_existed = exclusion_db.is_file()
    pending_db = backup_dir / "cognitive-search-exclusions.pending.db"
    if target_existed:
        report["backup"] = _backup_sqlite(
            exclusion_db,
            backup_dir / "cognitive-search-exclusions.before.db",
        )
        working_db = exclusion_db
    else:
        working_db = pending_db

    committed = False
    published = False
    try:
        connection = sqlite3.connect(working_db)
        try:
            if not target_existed:
                initialize_search_exclusion_ledger(connection)
                connection.commit()
            elif not validate_search_exclusion_ledger(connection)["ok"]:
                raise RuntimeError("cognitive search exclusion ledger validation failed")
            connection.execute("BEGIN IMMEDIATE")
            for index, candidate in enumerate(factory()):
                status = insert_search_exclusion(connection, candidate)
                report[f"{status}_count"] += 1
                if failpoint == "after_first_insert" and index == 0:
                    raise RuntimeError("injected exclusion reconciliation failure")

            fresh_inventory = inventory_search_exclusions(factory())
            if (
                fresh_inventory["inventory_hash"] != inventory["inventory_hash"]
                or fresh_inventory["object_manifest_hash"] != inventory["object_manifest_hash"]
            ):
                raise RuntimeError("source inventory changed during reconciliation")
            final_coverage = search_exclusion_coverage(
                factory(),
                exclusion_keys=_connection_keys(connection),
            )
            if final_coverage["uncovered_count"]:
                raise RuntimeError("cognitive search exclusions remain uncovered")
            if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("cognitive search exclusion ledger integrity failed")
            connection.commit()
            committed = True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        if not target_existed:
            recovery_copy = backup_dir / "cognitive-search-exclusions.created.db"
            shutil.copy2(pending_db, recovery_copy)
            exclusion_db.parent.mkdir(parents=True, exist_ok=True)
            if exclusion_db.exists():
                raise FileExistsError("exclusion ledger appeared during reconciliation")
            os.replace(pending_db, exclusion_db)
            published = True
            report["backup"] = {
                "path": str(recovery_copy),
                "source_sha256": "absent",
                "backup_sha256": _sha256_file(recovery_copy),
                "integrity_check": "ok",
            }

        if failpoint == "before_final_verification":
            raise RuntimeError("injected committed exclusion verification failure")
        final_keys, final_validation = load_search_exclusion_keys(exclusion_db)
        final_coverage = search_exclusion_coverage(factory(), exclusion_keys=final_keys)
        if not final_validation.get("ok") or final_coverage["uncovered_count"]:
            raise RuntimeError("committed exclusion ledger failed final verification")
    except BaseException as exc:
        if committed or published:
            if target_existed:
                _restore_sqlite_backup(
                    backup_dir / "cognitive-search-exclusions.before.db",
                    exclusion_db,
                )
            else:
                _remove_sqlite_target(exclusion_db)
        manifest.update(
            {
                "status": "rolled_back",
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )
        _write_review_manifest(review_manifest, manifest)
        raise

    report["ledger_validation"] = final_validation
    report["covered_count"] = final_coverage["covered_count"]
    report["uncovered_count"] = final_coverage["uncovered_count"]
    report["would_insert_count"] = 0
    report["integrity_check"] = "ok"
    manifest.update(
        {
            "status": "committed",
            "inserted_count": report["inserted_count"],
            "existing_count": report["existing_count"],
            "covered_count": report["covered_count"],
            "target_sha256": _sha256_file(exclusion_db),
        }
    )
    _write_review_manifest(review_manifest, manifest)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--target",
        choices=("all", "wiki", "cognitive-graph", "evidence-graph"),
        help="required exact source class for --apply; dry-run defaults to all",
    )
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-inventory-hash")
    parser.add_argument("--expected-object-manifest-hash")
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--cognitive-graph-db", type=Path)
    parser.add_argument("--evidence-graph-db", type=Path)
    parser.add_argument("--exclusion-db", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.target is None:
        parser.error("--apply requires an explicit --target")
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    if args.apply and (not args.expected_inventory_hash or not args.expected_object_manifest_hash):
        parser.error("--apply requires both reviewed inventory hashes")

    config = get_config()
    database_dir = Path(config.database_dir)
    selected = args.target or "all"
    targets = {
        "all": ("wiki", "cognitive_graph", "evidence_graph"),
        "wiki": ("wiki",),
        "cognitive-graph": ("cognitive_graph",),
        "evidence-graph": ("evidence_graph",),
    }[selected]
    report = reconcile(
        targets=targets,
        wiki_dir=Path(args.wiki_dir or config.wiki_dir),
        cognitive_graph_db=Path(args.cognitive_graph_db or database_dir / "cognitive_graph.db"),
        evidence_graph_db=Path(args.evidence_graph_db or database_dir / "evidence_graph.db"),
        exclusion_db=Path(args.exclusion_db or database_dir / "cognitive_search_exclusions.db"),
        apply=args.apply,
        backup_dir=args.backup_dir,
        expected_inventory_hash=str(args.expected_inventory_hash or ""),
        expected_object_manifest_hash=str(args.expected_object_manifest_hash or ""),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "cognitive search exclusions: "
            f"candidates={report['candidate_count']} "
            f"uncovered={report['uncovered_count']} "
            f"mode={report['mode']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
