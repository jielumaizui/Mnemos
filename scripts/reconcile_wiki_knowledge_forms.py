#!/usr/bin/env python3
"""Reconcile historical Wiki knowledge forms through the lifecycle ledger.

Dry-run rebuilds the hash-bound form plan.  ``--apply`` requires the exact
dry-run plan hash and an empty backup directory.  Every Markdown preimage is
backed up before an atomic replacement; lifecycle/event publication is then
committed as one exact batch.  A failure restores every attempted page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.frontmatter import parse_frontmatter, write_frontmatter
from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock
from scripts.plan_wiki_knowledge_form_reconciliation import build_plan

SCHEMA_VERSION = "mnemos.wiki_knowledge_form_reconciliation.v1"


@dataclass(frozen=True)
class FormUpdate:
    path: Path
    relative_path: str
    before_sha256: str
    after_sha256: str
    content: str


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def prepare_updates(
    *,
    wiki_dir: Path,
    checkpoint_db: Path | None,
    review_manifest: Path,
    expected_plan_hash: str = "",
) -> tuple[dict[str, Any], list[FormUpdate]]:
    report = build_plan(
        wiki_dir=wiki_dir,
        checkpoint_db=checkpoint_db,
        review_manifest=review_manifest,
    )
    if not report["apply_ready"]:
        raise RuntimeError("knowledge-form reconciliation plan is not apply-ready")
    if expected_plan_hash and report["plan_hash"] != expected_plan_hash:
        raise RuntimeError(
            "knowledge-form plan hash drifted: "
            f"expected {expected_plan_hash}, got {report['plan_hash']}"
        )

    root = wiki_dir.expanduser().resolve(strict=True)
    updates: list[FormUpdate] = []
    for item in report["updates"]:
        relative_path = str(item["relative_path"])
        path = (root / relative_path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError(f"knowledge-form update escaped Wiki root: {relative_path}")
        before = path.read_bytes()
        before_sha256 = _sha256(before)
        if before_sha256 != item["before_sha256"]:
            raise RuntimeError(f"knowledge-form preimage drifted: {relative_path}")
        frontmatter, body = parse_frontmatter(before.decode("utf-8", errors="strict"))
        if frontmatter is None:
            raise ValueError(f"knowledge-form page has no frontmatter: {relative_path}")
        frontmatter["知识形态"] = str(item["form"])
        content = write_frontmatter(frontmatter, body)
        after_sha256 = _sha256(content.encode("utf-8"))
        if after_sha256 == before_sha256:
            raise RuntimeError(f"knowledge-form update was a no-op: {relative_path}")
        updates.append(
            FormUpdate(
                path=path,
                relative_path=relative_path,
                before_sha256=before_sha256,
                after_sha256=after_sha256,
                content=content,
            )
        )
    return report, updates


def apply_updates(
    *,
    config: Any,
    plan: dict[str, Any],
    updates: list[FormUpdate],
    backup_dir: Path,
    expected_plan_hash: str = "",
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
    projection_committer: Callable[..., dict[str, Any]] | None = None,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from scripts.reconcile_wiki_acl_projection import (
        ExactWikiProjectionUpdate,
        commit_exact_wiki_updates,
    )

    if not expected_plan_hash:
        raise ValueError("apply requires an exact expected plan hash")
    if str(plan.get("plan_hash") or "") != expected_plan_hash:
        raise RuntimeError("expected plan hash does not match prepared knowledge-form plan")
    wiki_dir = Path(config.wiki_dir).expanduser().resolve(strict=True)
    database_dir = Path(config.database_dir).expanduser().resolve(strict=True)
    backup_resolved = backup_dir.expanduser().resolve(strict=False)
    if backup_resolved == wiki_dir or backup_resolved.is_relative_to(wiki_dir):
        raise ValueError("backup directory must be outside the Wiki root")
    committer = projection_committer or commit_exact_wiki_updates
    with offline_migration_lock(database_dir, daemon_check=daemon_check):
        if backup_dir.exists() and (not backup_dir.is_dir() or any(backup_dir.iterdir())):
            raise ValueError("backup directory must not exist or must be empty")
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)

        manifest_path = backup_dir / "knowledge-form-reconciliation-manifest.json"
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "prepared",
            "source_plan_hash": plan["plan_hash"],
            "update_count": len(updates),
            "updates": [
                {
                    "relative_path": item.relative_path,
                    "before_sha256": item.before_sha256,
                    "after_sha256": item.after_sha256,
                }
                for item in updates
            ],
        }
        backups: dict[Path, Path] = {}
        for item in updates:
            if _sha256(item.path.read_bytes()) != item.before_sha256:
                raise RuntimeError(f"knowledge-form changed while preparing: {item.relative_path}")
            backup = backup_dir / "wiki" / item.relative_path
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.path, backup)
            if _sha256(backup.read_bytes()) != item.before_sha256:
                raise RuntimeError(f"knowledge-form backup hash mismatch: {item.relative_path}")
            staged = backup_dir / "staged" / item.relative_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(staged, item.content)
            if _sha256(staged.read_bytes()) != item.after_sha256:
                raise RuntimeError(f"knowledge-form staged hash mismatch: {item.relative_path}")
            backups[item.path] = backup
        _atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

        attempted: list[FormUpdate] = []
        try:
            for index, item in enumerate(updates, start=1):
                if _sha256(item.path.read_bytes()) != item.before_sha256:
                    raise RuntimeError(f"knowledge-form changed before write: {item.relative_path}")
                attempted.append(item)
                staged = backup_dir / "staged" / item.relative_path
                _atomic_write(item.path, staged.read_text(encoding="utf-8"))
                if failpoint is not None:
                    failpoint(f"after_wiki_write:{index}")
                if _sha256(item.path.read_bytes()) != item.after_sha256:
                    raise RuntimeError(
                        f"knowledge-form write verification failed: {item.relative_path}"
                    )
            manifest["status"] = "source_materialized"
            _atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            if failpoint is not None:
                failpoint("before_projection_commit")
            projection = committer(
                config=config,
                updates=(
                    ExactWikiProjectionUpdate(
                        path=item.path,
                        before_sha256=item.before_sha256,
                        after_sha256=item.after_sha256,
                    )
                    for item in updates
                ),
                backup_dir=backup_dir / "wiki-projection",
                source="cog016_knowledge_form_reconciliation",
            )
            if not projection.get("ok"):
                raise RuntimeError("knowledge-form projection commit did not converge")
            manifest["status"] = "projection_committed"
            manifest["wiki_projection"] = projection
            _atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            if failpoint is not None:
                failpoint("after_projection_commit")
        except BaseException as exc:
            rollback_errors: list[str] = []
            projection_recovery: dict[str, Any] = {
                "found": False,
                "status": "absent",
            }
            projection_backup = backup_dir / "wiki-projection"
            if projection_backup.is_dir():
                try:
                    from scripts.reconcile_wiki_acl_projection import (
                        _recover_wiki_projection_databases_unlocked,
                    )

                    projection_recovery = _recover_wiki_projection_databases_unlocked(
                        config=config,
                        backup_dir=projection_backup,
                        manifest_name="wiki-projection-batch-manifest.json",
                    )
                except BaseException as projection_rollback_exc:
                    rollback_errors.append(f"wiki_projection: {projection_rollback_exc}")
            for item in reversed(attempted):
                try:
                    _atomic_write(
                        item.path,
                        backups[item.path].read_text(encoding="utf-8"),
                    )
                    if _sha256(item.path.read_bytes()) != item.before_sha256:
                        raise RuntimeError("restored source hash mismatch")
                except BaseException as rollback_exc:
                    rollback_errors.append(f"{item.relative_path}: {rollback_exc}")
            manifest.update(
                {
                    "status": "rollback_failed" if rollback_errors else "rolled_back",
                    "failure": f"{type(exc).__name__}: {exc}",
                    "rollback_errors": rollback_errors,
                    "projection_recovery": projection_recovery,
                }
            )
            _atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            detail = "; ".join(rollback_errors) if rollback_errors else "all files restored"
            raise RuntimeError(f"knowledge-form batch failed; {detail}") from exc

        manifest["status"] = "committed"
        _atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "changed": len(updates),
            "source_plan_hash": plan["plan_hash"],
            "backup_manifest": str(manifest_path),
            "wiki_projection": projection,
            "apply_without_exact_plan_hash": 0,
            "wiki_write_before_offline_lock": 0,
            "partial_generation_visible": 0,
            "wiki_projection_generation_mismatch": 0,
        }


def recover_incomplete_generation(
    *,
    config: Any,
    backup_dir: Path,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
) -> dict[str, Any]:
    """Restore a prepared form batch after process death, before replanning."""

    backup_resolved = backup_dir.expanduser().resolve(strict=False)
    manifest_path = backup_resolved / "knowledge-form-reconciliation-manifest.json"
    if not manifest_path.is_file():
        return {"found": False, "status": "absent"}
    database_dir = Path(config.database_dir).expanduser().resolve(strict=True)
    wiki_dir = Path(config.wiki_dir).expanduser().resolve(strict=True)
    with offline_migration_lock(database_dir, daemon_check=daemon_check):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        status = str(manifest.get("status") or "")
        if status in {"committed", "rolled_back", "recovered"}:
            return {"found": True, "status": status}
        if status not in {
            "prepared",
            "source_materialized",
            "projection_committed",
            "rollback_failed",
        }:
            raise RuntimeError(f"knowledge-form recovery status is unsupported: {status}")
        projection_backup = backup_resolved / "wiki-projection"
        projection_recovery: dict[str, Any] = {
            "found": False,
            "status": "absent",
        }
        if projection_backup.is_dir():
            from scripts.reconcile_wiki_acl_projection import (
                _recover_wiki_projection_databases_unlocked,
            )

            projection_recovery = _recover_wiki_projection_databases_unlocked(
                config=config,
                backup_dir=projection_backup,
                manifest_name="wiki-projection-batch-manifest.json",
            )
        for item in manifest.get("updates") or ():
            relative_path = str(item["relative_path"])
            target = (wiki_dir / relative_path).resolve(strict=False)
            if not target.is_relative_to(wiki_dir):
                raise RuntimeError("knowledge-form recovery path escaped Wiki root")
            backup = (backup_resolved / "wiki" / relative_path).resolve(strict=True)
            if not backup.is_relative_to(backup_resolved):
                raise RuntimeError("knowledge-form recovery backup escaped generation root")
            if _sha256(backup.read_bytes()) != str(item["before_sha256"]):
                raise RuntimeError("knowledge-form recovery backup hash mismatch")
            _atomic_write(target, backup.read_text(encoding="utf-8"))
            if _sha256(target.read_bytes()) != str(item["before_sha256"]):
                raise RuntimeError("knowledge-form recovery source hash mismatch")
        manifest.update(
            {
                "status": "recovered",
                "projection_recovery": projection_recovery,
            }
        )
        _atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return {
            "found": True,
            "status": "recovered",
            "projection_recovery": projection_recovery,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument("--review-manifest", type=Path, required=True)
    parser.add_argument("--checkpoint-db", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.recover:
        parser.error("--apply and --recover are mutually exclusive")
    if (args.apply or args.recover) and args.backup_dir is None:
        parser.error("--apply/--recover requires --backup-dir")
    if args.apply and not args.expected_plan_hash:
        parser.error("--apply requires --expected-plan-hash")
    try:
        from core.config import get_config

        config = get_config()
        if args.recover:
            report = recover_incomplete_generation(
                config=config,
                backup_dir=args.backup_dir,
            )
            report.update({"schema_version": SCHEMA_VERSION, "ok": True})
        else:
            plan, updates = prepare_updates(
                wiki_dir=args.wiki_dir,
                checkpoint_db=args.checkpoint_db
                or Path(config.database_dir) / "distillation_chunks.db",
                review_manifest=args.review_manifest,
                expected_plan_hash=args.expected_plan_hash,
            )
            if args.apply:
                report = apply_updates(
                    config=config,
                    plan=plan,
                    updates=updates,
                    backup_dir=args.backup_dir,
                    expected_plan_hash=args.expected_plan_hash,
                )
            else:
                report = {
                    "schema_version": SCHEMA_VERSION,
                    "ok": True,
                    "changed": 0,
                    "would_change": len(updates),
                    "source_plan_hash": plan["plan_hash"],
                }
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        report = {"schema_version": SCHEMA_VERSION, "ok": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
