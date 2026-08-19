#!/usr/bin/env python3
"""Reconcile the ACL-safe Wiki ANN corpus without provider calls when possible."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.embeddings.index_manager import EmbeddingIndexManager  # noqa: E402
from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock  # noqa: E402
from scripts.wiki_projection_ann_audit import (  # noqa: E402
    compare_retained_hnsw_vectors,
    wiki_label_map,
)


def _sha256_path(path: Path) -> str:
    if not path.is_file():
        return ""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _validate_backup_dir(backup_dir: Path, *, wiki_dir: Path, index_dir: Path) -> None:
    if backup_dir.exists() and not backup_dir.is_dir():
        raise ValueError("backup path exists and is not a directory")
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise ValueError("backup directory must not exist or must be empty")
    resolved = backup_dir.resolve(strict=False)
    for protected in (wiki_dir.resolve(strict=False), index_dir.resolve(strict=False)):
        if resolved == protected or resolved in protected.parents or protected in resolved.parents:
            raise ValueError("backup directory must be disjoint from Wiki and index roots")


def _eligible_label_map(
    manager: EmbeddingIndexManager,
    meta_path: Path,
) -> dict[str, int]:
    eligible_pages, _excluded = manager._scan_indexable_wiki_pages()
    eligible = {page.relative_to(manager.wiki_base).as_posix() for page in eligible_pages}
    return {
        key: label
        for key, label in wiki_label_map(meta_path).items()
        if key.split("\0", 1)[0] in eligible
    }


def _backup_artifacts(*, index_path: Path, meta_path: Path, backup_dir: Path) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    records: dict[str, Any] = {}
    for source in (index_path, meta_path):
        target = backup_dir / source.name
        existed = source.is_file()
        if existed:
            shutil.copy2(source, target)
            if _sha256_path(source) != _sha256_path(target):
                raise RuntimeError(f"backup hash mismatch: {source.name}")
        records[source.name] = {
            "existed": existed,
            "source_sha256": _sha256_path(source),
            "backup_sha256": _sha256_path(target),
        }
    return records


def _restore_artifacts(
    *, index_path: Path, meta_path: Path, backup_dir: Path, backup: dict[str, Any]
) -> dict[str, Any]:
    restored: dict[str, Any] = {}
    for target in (index_path, meta_path):
        record = backup[target.name]
        source = backup_dir / target.name
        if bool(record["existed"]):
            shutil.copy2(source, target)
        else:
            target.unlink(missing_ok=True)
        actual = _sha256_path(target)
        expected = str(record["source_sha256"])
        if actual != expected:
            raise RuntimeError(f"rollback hash mismatch: {target.name}")
        restored[target.name] = actual
    return restored


def reconcile(
    *,
    apply: bool,
    backup_dir: Path | None = None,
    reviewed_plan_hash: str = "",
) -> dict[str, Any]:
    """Preview or apply one exact, rollback-safe Wiki ANN reconciliation."""

    config = get_config()
    if not apply:
        return _reconcile_unlocked(
            apply=False,
            backup_dir=backup_dir,
            reviewed_plan_hash=reviewed_plan_hash,
            config=config,
        )
    with offline_migration_lock(
        Path(config.database_dir),
        daemon_check=runtime_writers_are_inactive,
    ):
        return _reconcile_unlocked(
            apply=True,
            backup_dir=backup_dir,
            reviewed_plan_hash=reviewed_plan_hash,
            config=config,
        )


def _reconcile_unlocked(
    *,
    apply: bool,
    backup_dir: Path | None,
    reviewed_plan_hash: str,
    config: Any,
) -> dict[str, Any]:
    wiki_dir = Path(config.wiki_dir).expanduser()
    database_dir = Path(config.database_dir).expanduser()
    index_dir = database_dir / "embedding_index"
    manager = EmbeddingIndexManager(
        wiki_base=wiki_dir,
        index_dir=index_dir,
        config=config,
    )
    manager.client = None
    plan = manager.reconciliation_plan(force_full=False)
    report: dict[str, Any] = {
        "schema_version": "mnemos.wiki_search_index_reconciliation_run.v1",
        "mode": "apply" if apply else "dry_run",
        "plan": plan,
        "applied": False,
        "ok": not bool(plan["provider_required_chunk_count"]),
    }
    if not apply:
        return report
    if backup_dir is None:
        raise ValueError("apply requires an explicit backup directory")
    if reviewed_plan_hash != plan["plan_hash"]:
        raise ValueError("reviewed plan hash does not match the current reconciliation plan")
    if int(plan["provider_required_chunk_count"]):
        raise RuntimeError(
            "reconciliation requires provider embeddings; explicit real-api is required"
        )
    _validate_backup_dir(backup_dir, wiki_dir=wiki_dir, index_dir=index_dir)

    index_path = index_dir / "wiki_index.bin"
    meta_path = index_dir / "wiki_meta.json"
    backup = _backup_artifacts(
        index_path=index_path,
        meta_path=meta_path,
        backup_dir=backup_dir,
    )
    manifest_path = backup_dir / "wiki-search-index-reconciliation-manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "mnemos.wiki_search_index_reconciliation_manifest.v1",
        "status": "prepared",
        "plan": plan,
        "backup": backup,
    }
    _atomic_json(manifest_path, manifest)
    diagnostics: dict[str, Any] = {}
    try:
        before_labels = _eligible_label_map(manager, backup_dir / meta_path.name)
        build = manager.build_index(force_full=False)
        # Verify only from artifacts reloaded by a fresh process view.  The
        # mutating manager's memory state cannot certify that metadata reached
        # durable storage.
        persisted_manager = EmbeddingIndexManager(
            wiki_base=wiki_dir,
            index_dir=index_dir,
            config=config,
        )
        persisted_manager.client = None
        audit = persisted_manager.audit_coverage()
        after_plan = persisted_manager.reconciliation_plan(force_full=False)
        if plan["backend"] == "hnswlib":
            vector_comparison = compare_retained_hnsw_vectors(
                backup_dir / index_path.name,
                index_path,
                retained_before_labels=before_labels,
                after_labels=wiki_label_map(meta_path),
            )
        else:
            vector_comparison = {
                "schema_version": "mnemos.memory_retained_vector_comparison.v1",
                "equal": (
                    plan["eligible_manifest_sha256"] == after_plan["eligible_manifest_sha256"]
                ),
            }
        converged = not any(
            int(after_plan[key])
            for key in ("add_page_count", "update_page_count", "remove_page_count")
        )
        diagnostics = {
            "build": build,
            "audit": audit,
            "after_plan": after_plan,
            "vector_comparison": vector_comparison,
            "converged": converged,
        }
        ok = bool(
            build.get("status") in {"ok", "no_change"}
            and int(build.get("provider_required_chunks", 0)) == 0
            and audit.get("ok")
            and converged
            and vector_comparison.get("equal")
        )
        if not ok:
            raise RuntimeError("Wiki ANN reconciliation comparator did not converge")
        manifest.update(
            {
                "status": "committed",
                "build": build,
                "audit": audit,
                "after_plan": after_plan,
                "vector_comparison": vector_comparison,
            }
        )
        _atomic_json(manifest_path, manifest)
        report.update(
            {
                "applied": True,
                "ok": True,
                "backup_dir": str(backup_dir),
                "manifest": str(manifest_path),
                "build": build,
                "audit": audit,
                "after_plan": after_plan,
                "vector_comparison": vector_comparison,
            }
        )
        return report
    except BaseException as exc:
        restored = _restore_artifacts(
            index_path=index_path,
            meta_path=meta_path,
            backup_dir=backup_dir,
            backup=backup,
        )
        manifest.update(
            {
                "status": "rolled_back",
                "failure": f"{type(exc).__name__}: {exc}",
                "diagnostics": diagnostics,
                "restored": restored,
            }
        )
        _atomic_json(manifest_path, manifest)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--reviewed-plan-hash", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = reconcile(
            apply=args.apply,
            backup_dir=args.backup_dir,
            reviewed_plan_hash=args.reviewed_plan_hash,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        result = {
            "schema_version": "mnemos.wiki_search_index_reconciliation_run.v1",
            "mode": "apply" if args.apply else "dry_run",
            "applied": False,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(result["error"], file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "Wiki search index reconciliation: "
            f"mode={result['mode']} ok={result['ok']} "
            f"plan={result['plan']['plan_hash']}"
        )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
