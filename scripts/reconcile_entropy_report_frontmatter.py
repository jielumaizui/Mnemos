#!/usr/bin/env python3
"""Compact unbounded entropy-report provenance with exact recovery backups."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence, TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.access_policy import ACLReconciler
from core.config import get_config
from core.frontmatter import read_strict_frontmatter_document, write_frontmatter
from core.kia.kia_event_consumer import compact_entropy_report_frontmatter
from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive
from core.ops.offline_migration_lock import offline_migration_lock
from scripts.reconcile_wiki_acl_projection import (
    ExactWikiProjectionUpdate,
    commit_exact_wiki_updates,
    recover_wiki_projection_databases,
)


@dataclass(frozen=True)
class _EntropyPlanItem:
    path: Path
    content: str
    original_sha256: str
    desired_sha256: str


class _EntropyReconcileReport(TypedDict):
    schema_version: str
    wiki_dir: str
    scanned_count: int
    entropy_report_count: int
    would_change: int
    changed: int
    oversized_before_count: int
    parse_error_count: int
    parse_errors: list[dict[str, str]]
    backup_dir: str
    backup_manifest: str
    wiki_projection: dict[str, Any]
    mode: str


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_relative_path(value: Any) -> Path:
    relative = Path(str(value))
    if (
        not str(value)
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError("entropy recovery manifest contains an unsafe relative path")
    return relative


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    ACLReconciler._atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def recover_entropy_reconciliation(
    wiki_dir: Path,
    *,
    backup_dir: Path,
    projection_config: Any,
    daemon_check=runtime_writers_are_inactive,
) -> dict[str, Any]:
    """Restore a prepared entropy batch and its unconfirmed projections."""

    database_dir = Path(projection_config.database_dir)
    with offline_migration_lock(database_dir, daemon_check=daemon_check):
        wiki_root = Path(wiki_dir).expanduser().resolve(strict=False)
        root = Path(backup_dir).expanduser().resolve(strict=True)
        manifest_path = root / "entropy-frontmatter-reconciliation-manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("entropy recovery backup lacks its prepared manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
        if (
            manifest.get("schema_version")
            != "mnemos.entropy_report_frontmatter_reconcile_manifest.v1"
        ):
            raise RuntimeError("entropy recovery manifest schema is unsupported")
        status = str(manifest.get("status") or "")
        if status in {"rolled_back", "recovered_rollback"}:
            return {
                "schema_version": "mnemos.entropy_report_frontmatter_recovery.v1",
                "status": status,
                "restored_file_count": 0,
                "backup_dir": str(root),
            }
        if status == "committed":
            raise RuntimeError("refusing to recover a committed entropy reconciliation")
        if status not in {"prepared", "rollback_failed", "recovery_failed"}:
            raise RuntimeError(f"entropy recovery status is unsupported: {status}")
        records = manifest.get("files")
        if not isinstance(records, list) or not records:
            raise RuntimeError("entropy recovery manifest contains no file records")
        prepared: list[tuple[Path, Path, str]] = []
        seen: set[str] = set()
        for raw_record in records:
            if not isinstance(raw_record, dict):
                raise RuntimeError("entropy recovery manifest contains an invalid file record")
            relative = _safe_relative_path(raw_record.get("relative_path"))
            relative_key = relative.as_posix()
            if relative_key in seen:
                raise RuntimeError("entropy recovery manifest contains duplicate file records")
            seen.add(relative_key)
            target = (wiki_root / relative).resolve(strict=False)
            backup = (root / "wiki" / relative).resolve(strict=False)
            if not target.is_relative_to(wiki_root) or not backup.is_relative_to(root / "wiki"):
                raise RuntimeError("entropy recovery path escaped its declared root")
            original_sha256 = str(raw_record.get("original_sha256") or "")
            if not backup.is_file() or _sha256_bytes(backup.read_bytes()) != original_sha256:
                raise RuntimeError(f"entropy recovery backup hash mismatch: {relative}")
            prepared.append((target, backup, original_sha256))
        try:
            projection_dir = root / "wiki-projection"
            projection = {"found": False, "status": "absent"}
            if projection_dir.exists():
                projection = recover_wiki_projection_databases(
                    config=projection_config,
                    backup_dir=projection_dir,
                    manifest_name="wiki-projection-batch-manifest.json",
                )
            for target, backup, original_sha256 in prepared:
                ACLReconciler._atomic_write_text(
                    target,
                    backup.read_bytes().decode("utf-8", errors="strict"),
                )
                if _sha256_bytes(target.read_bytes()) != original_sha256:
                    raise RuntimeError(f"entropy recovery verification failed: {target}")
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
            "schema_version": "mnemos.entropy_report_frontmatter_recovery.v1",
            "status": "recovered_rollback",
            "restored_file_count": len(prepared),
            "wiki_projection": projection,
            "backup_dir": str(root),
        }


def reconcile_entropy_reports(
    wiki_dir: Path,
    *,
    apply: bool = False,
    backup_dir: Path | None = None,
    projection_config: Any | None = None,
    daemon_check=runtime_writers_are_inactive,
) -> _EntropyReconcileReport:
    """Plan or apply while holding the full offline migration lifetime lock."""

    if not apply:
        return _reconcile_entropy_reports_unlocked(
            wiki_dir,
            apply=False,
            backup_dir=backup_dir,
            projection_config=projection_config,
        )
    if projection_config is None:
        raise ValueError("entropy report apply requires lifecycle/event projection config")
    with offline_migration_lock(
        Path(projection_config.database_dir),
        daemon_check=daemon_check,
    ):
        return _reconcile_entropy_reports_unlocked(
            wiki_dir,
            apply=True,
            backup_dir=backup_dir,
            projection_config=projection_config,
        )


def _reconcile_entropy_reports_unlocked(
    wiki_dir: Path,
    *,
    apply: bool = False,
    backup_dir: Path | None = None,
    projection_config: Any | None = None,
) -> _EntropyReconcileReport:
    wiki_dir = Path(wiki_dir)
    if apply and backup_dir is None:
        raise ValueError("entropy report apply requires backup_dir")
    report: _EntropyReconcileReport = {
        "schema_version": "mnemos.entropy_report_frontmatter_reconcile.v1",
        "wiki_dir": str(wiki_dir),
        "scanned_count": 0,
        "entropy_report_count": 0,
        "would_change": 0,
        "changed": 0,
        "oversized_before_count": 0,
        "parse_error_count": 0,
        "parse_errors": [],
        "backup_dir": str(backup_dir or ""),
        "backup_manifest": "",
        "wiki_projection": {},
        "mode": "apply" if apply else "dry_run",
    }
    plan: list[_EntropyPlanItem] = []
    entropy_dir = wiki_dir / "06-Retrospectives" / "entropy"
    if not entropy_dir.is_dir():
        return report
    for path in sorted(entropy_dir.glob("entropy-suggestions-*.md")):
        report["scanned_count"] += 1
        try:
            frontmatter, body, content = read_strict_frontmatter_document(
                path,
                errors="strict",
            )
        except (OSError, UnicodeError, ValueError) as exc:
            report["parse_error_count"] += 1
            report["parse_errors"].append(
                {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if str(frontmatter.get("report_type") or "") != "entropy_suggestions":
            continue
        report["entropy_report_count"] += 1
        closing = content.find("---", 3)
        if closing > 0 and len(content[3:closing].encode("utf-8")) > 65536:
            report["oversized_before_count"] += 1
        compacted = compact_entropy_report_frontmatter(frontmatter)
        if compacted == frontmatter:
            continue
        report["would_change"] += 1
        rendered = write_frontmatter(compacted, body)
        plan.append(
            _EntropyPlanItem(
                path=path,
                content=rendered,
                original_sha256=_sha256_bytes(path.read_bytes()),
                desired_sha256=_sha256_bytes(rendered.encode("utf-8")),
            )
        )
    if not apply:
        return report
    if report["parse_error_count"]:
        raise ValueError(
            "entropy report apply refused because the dry-run plan contains parse errors"
        )
    if not plan:
        report["backup_manifest"] = ""
        return report
    if projection_config is None:
        raise ValueError("entropy report apply requires lifecycle/event projection config")

    assert backup_dir is not None
    backup_dir = Path(backup_dir)
    if backup_dir.exists() and (not backup_dir.is_dir() or any(backup_dir.iterdir())):
        raise ValueError("entropy backup directory must not exist or must be empty")
    wiki_resolved = wiki_dir.expanduser().resolve(strict=True)
    backup_resolved = backup_dir.expanduser().resolve(strict=False)
    if backup_resolved == wiki_resolved or backup_resolved.is_relative_to(wiki_resolved):
        raise ValueError("entropy backup directory must be outside the Wiki")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)

    backups: dict[Path, Path] = {}
    manifest_files: list[dict[str, str]] = []
    for item in plan:
        if _sha256_bytes(item.path.read_bytes()) != item.original_sha256:
            raise RuntimeError(f"entropy report changed while planning: {item.path}")
        relative = item.path.relative_to(wiki_dir)
        backup_path = backup_dir / "wiki" / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.path, backup_path)
        if _sha256_bytes(backup_path.read_bytes()) != item.original_sha256:
            raise RuntimeError(f"entropy report backup hash mismatch: {backup_path}")
        backups[item.path] = backup_path
        manifest_files.append(
            {
                "relative_path": relative.as_posix(),
                "original_sha256": item.original_sha256,
                "desired_sha256": item.desired_sha256,
            }
        )

    manifest_path = backup_dir / "entropy-frontmatter-reconciliation-manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "mnemos.entropy_report_frontmatter_reconcile_manifest.v1",
        "status": "prepared",
        "files": manifest_files,
    }
    ACLReconciler._atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    attempted: list[_EntropyPlanItem] = []
    try:
        for item in plan:
            if _sha256_bytes(item.path.read_bytes()) != item.original_sha256:
                raise RuntimeError(f"entropy report changed before replace: {item.path}")
            attempted.append(item)
            ACLReconciler._atomic_write_text(item.path, item.content)
            if _sha256_bytes(item.path.read_bytes()) != item.desired_sha256:
                raise RuntimeError(f"entropy report verification failed: {item.path}")
        projection = commit_exact_wiki_updates(
            config=projection_config,
            updates=(
                ExactWikiProjectionUpdate(
                    path=item.path,
                    before_sha256=item.original_sha256,
                    after_sha256=item.desired_sha256,
                )
                for item in plan
            ),
            backup_dir=backup_dir / "wiki-projection",
            source="cog015_entropy_frontmatter_reconciliation",
        )
    except BaseException as exc:
        rollback_errors: list[str] = []
        for item in reversed(attempted):
            try:
                ACLReconciler._atomic_write_text(
                    item.path,
                    backups[item.path].read_bytes().decode("utf-8", errors="strict"),
                )
                if _sha256_bytes(item.path.read_bytes()) != item.original_sha256:
                    raise RuntimeError("restored source hash mismatch")
            except BaseException as rollback_exc:
                rollback_errors.append(f"{item.path}: {rollback_exc}")
        manifest.update(
            {
                "status": "rollback_failed" if rollback_errors else "rolled_back",
                "failure": f"{type(exc).__name__}: {exc}",
                "rollback_errors": rollback_errors,
            }
        )
        ACLReconciler._atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        detail = "; ".join(rollback_errors) if rollback_errors else "all files restored"
        raise RuntimeError(f"entropy report batch failed; {detail}") from exc

    manifest.update({"status": "committed", "wiki_projection": projection})
    ACLReconciler._atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    report["changed"] = len(plan)
    report["backup_manifest"] = str(manifest_path)
    report["wiki_projection"] = projection
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--recover-backup-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.recover_backup_dir is not None and (args.apply or args.backup_dir is not None):
        parser.error("--recover-backup-dir cannot be combined with apply options")
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    if args.backup_dir is not None and args.backup_dir.exists() and any(args.backup_dir.iterdir()):
        parser.error("--backup-dir must not exist or must be empty")
    config = get_config()
    wiki_dir = args.wiki_dir or Path(config.wiki_dir)
    if args.recover_backup_dir is not None:
        try:
            recovery_report = recover_entropy_reconciliation(
                wiki_dir,
                backup_dir=args.recover_backup_dir,
                projection_config=config,
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(recovery_report, ensure_ascii=False, sort_keys=True))
        else:
            print(f"entropy recovery: {recovery_report['status']}")
        return 0
    reconciliation_report = reconcile_entropy_reports(
        wiki_dir,
        apply=args.apply,
        backup_dir=args.backup_dir,
        projection_config=config if args.apply else None,
    )
    if args.json:
        print(json.dumps(reconciliation_report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"entropy reports: {reconciliation_report['would_change']} change(s), "
            f"{reconciliation_report['parse_error_count']} parse error(s)"
        )
    return 1 if reconciliation_report["parse_error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
