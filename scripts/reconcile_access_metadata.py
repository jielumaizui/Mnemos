#!/usr/bin/env python3
"""Backfill provable ACL metadata and restrict unknown legacy items."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.access_policy import ACLReconciler, WikiProjectionBatchReceipt
from core.config import get_config
from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock


def _safe_relative_path(value: Any) -> Path:
    relative = Path(str(value))
    if not str(value) or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("ACL recovery manifest contains an unsafe relative path")
    return relative


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    ACLReconciler._atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def recover_acl_reconciliation(
    *,
    config: Any,
    backup_dir: Path,
    daemon_check=runtime_writers_are_inactive,
) -> dict[str, Any]:
    """Restore a prepared ACL batch left behind by process death."""

    database_dir = Path(config.database_dir)
    with offline_migration_lock(database_dir, daemon_check=daemon_check):
        root = Path(backup_dir).expanduser().resolve(strict=True)
        manifest_path = root / "acl-reconciliation-manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("ACL recovery backup lacks its prepared manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
        if manifest.get("schema_version") != "mnemos.acl_reconciliation_backup.v1":
            raise RuntimeError("ACL recovery manifest schema is unsupported")
        status = str(manifest.get("status") or "")
        if status in {"rolled_back", "recovered_rollback"}:
            return {
                "schema_version": "mnemos.acl_reconciliation_recovery.v1",
                "status": status,
                "restored_file_count": 0,
                "backup_dir": str(root),
            }
        if status == "committed":
            raise RuntimeError("refusing to recover a committed ACL reconciliation")
        if status not in {"prepared", "rollback_failed", "recovery_failed"}:
            raise RuntimeError(f"ACL recovery status is unsupported: {status}")
        records = manifest.get("files")
        if not isinstance(records, list) or not records:
            raise RuntimeError("ACL recovery manifest contains no file records")
        roots = {
            "wiki": Path(config.wiki_dir).expanduser().resolve(strict=False),
            "raw": Path(config.obsidian_vault_path).expanduser().resolve(strict=False),
        }
        prepared: list[tuple[Path, Path, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_record in records:
            if not isinstance(raw_record, dict):
                raise RuntimeError("ACL recovery manifest contains an invalid file record")
            kind = str(raw_record.get("kind") or "")
            if kind not in roots:
                raise RuntimeError(f"ACL recovery manifest has an invalid file kind: {kind}")
            relative = _safe_relative_path(raw_record.get("relative_path"))
            key = (kind, relative.as_posix())
            if key in seen:
                raise RuntimeError("ACL recovery manifest contains duplicate file records")
            seen.add(key)
            target = (roots[kind] / relative).resolve(strict=False)
            backup = (root / kind / relative).resolve(strict=False)
            if not target.is_relative_to(roots[kind]) or not backup.is_relative_to(root / kind):
                raise RuntimeError("ACL recovery path escaped its declared root")
            original_sha256 = str(raw_record.get("original_sha256") or "")
            if not backup.is_file() or ACLReconciler._sha256_path(backup) != original_sha256:
                raise RuntimeError(f"ACL recovery backup hash mismatch: {kind}/{relative}")
            prepared.append((target, backup, original_sha256))
        try:
            projection_dir = root / "wiki-projection"
            projection = {"found": False, "status": "absent"}
            if projection_dir.exists():
                from scripts.reconcile_wiki_acl_projection import (
                    recover_wiki_projection_databases,
                )

                projection = recover_wiki_projection_databases(
                    config=config,
                    backup_dir=projection_dir,
                    manifest_name="wiki-acl-projection-reconciliation-manifest.json",
                )
            for target, backup, original_sha256 in prepared:
                ACLReconciler._atomic_write_text(
                    target,
                    backup.read_bytes().decode("utf-8", errors="strict"),
                )
                if ACLReconciler._sha256_path(target) != original_sha256:
                    raise RuntimeError(f"ACL recovery verification failed: {target}")
        except BaseException as exc:
            manifest.update(
                {
                    "status": "recovery_failed",
                    "recovery_failure": f"{type(exc).__name__}: {exc}",
                }
            )
            _write_manifest(manifest_path, manifest)
            raise
        manifest.update(
            {
                "status": "recovered_rollback",
                "recovery": {
                    "restored_file_count": len(prepared),
                    "wiki_projection": projection,
                },
            }
        )
        _write_manifest(manifest_path, manifest)
        return {
            "schema_version": "mnemos.acl_reconciliation_recovery.v1",
            "status": "recovered_rollback",
            "restored_file_count": len(prepared),
            "wiki_projection": projection,
            "backup_dir": str(root),
        }


def main(argv: list[str] | None = None) -> int:
    """Run ACL reconciliation in dry-run or explicit apply mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write reconciled ACL fields; default is a read-only dry run",
    )
    parser.add_argument(
        "--target",
        choices=("all", "wiki", "raw"),
        help=(
            "asset class to reconcile; required for --apply and provenance is still "
            "read from canonical raw"
        ),
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="required new/empty recovery directory for --apply",
    )
    parser.add_argument(
        "--rebuild-raw-index",
        action="store_true",
        help="after --apply, rebuild the raw search index from reconciled files",
    )
    parser.add_argument(
        "--recover-backup-dir",
        type=Path,
        help="restore a prepared/crashed apply from this exact backup directory",
    )
    args = parser.parse_args(argv)
    if args.recover_backup_dir is not None and (
        args.apply or args.target is not None or args.backup_dir is not None or args.rebuild_raw_index
    ):
        parser.error("--recover-backup-dir cannot be combined with apply options")
    if args.rebuild_raw_index and not args.apply:
        parser.error("--rebuild-raw-index requires --apply")
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    if args.apply and args.target is None:
        parser.error("--apply requires an explicit --target")
    if args.backup_dir is not None and args.backup_dir.exists() and any(args.backup_dir.iterdir()):
        parser.error("--backup-dir must not exist or must be empty")

    config = get_config()
    if args.recover_backup_dir is not None:
        try:
            recovered = recover_acl_reconciliation(
                config=config,
                backup_dir=args.recover_backup_dir,
                daemon_check=runtime_writers_are_inactive,
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(json.dumps(recovered, ensure_ascii=False, sort_keys=True))
        return 0
    selected_target = args.target or "all"

    def commit_wiki_projection(
        acl_backup_dir: Path,
        expected_update_count: int,
    ) -> WikiProjectionBatchReceipt:
        from scripts.reconcile_wiki_acl_projection import reconcile

        preview = reconcile(
            apply=False,
            acl_backup_dir=acl_backup_dir,
            config=config,
        )
        plan = preview["plan"]
        if not preview["ok"] or int(plan["needs_mutation_count"]) != int(expected_update_count):
            raise RuntimeError("Wiki ACL lifecycle plan does not match the exact file batch")
        applied = reconcile(
            apply=True,
            acl_backup_dir=acl_backup_dir,
            backup_dir=acl_backup_dir / "wiki-projection",
            reviewed_plan_hash=str(plan["plan_hash"]),
            config=config,
        )
        diagnostics = applied["diagnostics"]
        event_validation = diagnostics["event_validation"]
        return WikiProjectionBatchReceipt(
            update_count=expected_update_count,
            mutation_count=int(diagnostics["scan"]["recorded_mutations"]),
            event_count=int(event_validation["found"]),
            backup_manifest=str(applied["manifest"]),
            source="cog015_acl_projection_reconciliation",
        )

    guard = (
        offline_migration_lock(
            Path(config.database_dir),
            daemon_check=runtime_writers_are_inactive,
        )
        if args.apply
        else nullcontext()
    )
    try:
        with guard:
            report: Dict[str, Any] = ACLReconciler(
                wiki_dir=config.wiki_dir,
                raw_dir=config.obsidian_vault_path,
                wiki_projection_commit=commit_wiki_projection,
            ).reconcile(
                apply=args.apply,
                targets=("wiki", "raw") if selected_target == "all" else (selected_target,),
                backup_dir=args.backup_dir,
            )

            if args.rebuild_raw_index:
                from core.app.raw_search import RawIndex

                with RawIndex(raw_dir=config.obsidian_vault_path) as index:
                    report["raw_index"] = index.sync_index(force_full=True)
    except RuntimeError as exc:
        parser.error(str(exc))

    report["mode"] = "apply" if args.apply else "dry_run"
    report["target"] = selected_target
    report["backup_dir"] = str(args.backup_dir or "")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["unresolved"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
