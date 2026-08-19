#!/usr/bin/env python3
"""Plan or initialize the independent COG-016 cognitive asset stores.

Legacy Persona JSON is intentionally not promoted: it lacks immutable source
authority, exact scope, expiry, and revision evidence.  The canonical runtime
reads only these new stores; the old projection remains historical evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cognitive.user_model_asset_store import (
    INTERACTION_PREFERENCE_SPEC,
    REGISTRY_DDL,
    REGISTRY_TABLE,
    USER_COGNITIVE_BLINDSPOT_SPEC,
    AssetStoreSpec,
    UserModelAssetStoreError,
    read_asset_store_state,
)
from core.config import get_config
from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock

SCHEMA_VERSION = "mnemos.user_model_asset_store_reconciliation.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _logical_database_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for line in conn.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _integrity(path: Path) -> tuple[bool, list[str]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [
            "|".join(str(item) for item in row) for row in conn.execute("PRAGMA foreign_key_check")
        ]
    return integrity == ["ok"], foreign_keys


def _backup(path: Path, target: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    integrity_ok, foreign_key_errors = _integrity(target)
    return {
        "source": str(path),
        "backup": str(target),
        "sha256": _sha256(target),
        "logical_hash": _logical_database_hash(target),
        "integrity_ok": integrity_ok,
        "foreign_key_errors": foreign_key_errors,
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _remove_sqlite_generation(path: Path) -> None:
    for target in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ):
        target.unlink(missing_ok=True)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _restore_pre_states(manifest: dict[str, Any]) -> None:
    for store in manifest["stores"]:
        target = Path(store["path"])
        if not store["existed"]:
            _remove_sqlite_generation(target)
            continue
        backup_path = Path(store["backup_path"])
        restore_temp = target.with_name(f".{target.name}.restore-{uuid4().hex}")
        shutil.copy2(backup_path, restore_temp)
        os.replace(restore_temp, target)
        _remove_sqlite_sidecars(target)
        if _logical_database_hash(target) != store["logical_hash"]:
            raise RuntimeError("restored asset store does not match pre-state hash")


def _restore_drill(manifest: dict[str, Any], generation_dir: Path) -> bool:
    for store in manifest["stores"]:
        if not store["existed"]:
            continue
        backup_path = Path(store["backup_path"])
        drill_path = generation_dir / f".restore-drill-{store['asset_type']}.sqlite"
        shutil.copy2(backup_path, drill_path)
        try:
            integrity_ok, foreign_key_errors = _integrity(drill_path)
            if (
                not integrity_ok
                or foreign_key_errors
                or _logical_database_hash(drill_path) != store["logical_hash"]
            ):
                return False
        finally:
            drill_path.unlink(missing_ok=True)
    return True


def _plan_one(path: Path, spec: AssetStoreSpec) -> dict[str, Any]:
    state = read_asset_store_state(path, spec)
    if state.status == "uninitialized":
        action = "initialize_fresh_canonical_store"
    elif state.ok:
        action = "none"
    else:
        action = "refuse_unknown_or_drifted_store"
    existed = path.is_file()
    integrity_ok = False
    foreign_key_errors: list[str] = []
    logical_hash = ""
    if existed:
        integrity_ok, foreign_key_errors = _integrity(path)
        logical_hash = _logical_database_hash(path)
    return {
        "path": str(path),
        "asset_type": spec.asset_type,
        "before": state.as_dict(),
        "planned_action": action,
        "legacy_active_promotion_count": 0,
        "existed": existed,
        "source_integrity_ok": integrity_ok if existed else True,
        "source_foreign_key_errors": foreign_key_errors,
        "source_logical_hash": logical_hash,
    }


def build_plan(
    *,
    user_cognitive_blindspot_db: Path,
    interaction_preference_db: Path,
) -> dict[str, Any]:
    stores = [
        _plan_one(user_cognitive_blindspot_db, USER_COGNITIVE_BLINDSPOT_SPEC),
        _plan_one(interaction_preference_db, INTERACTION_PREFERENCE_SPEC),
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "apply": False,
        "stores": stores,
        "legacy_active_promotion_count": 0,
        "refused_count": sum(
            item["planned_action"] == "refuse_unknown_or_drifted_store" for item in stores
        ),
        "changed": False,
        "asset_migration_without_plan_hash": 0,
    }
    report["plan_hash"] = _canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "stores": stores,
            "legacy_active_promotion_count": 0,
        }
    )
    return report


def _qualified_table_ddl(statement: str, *, schema: str, table: str) -> str:
    return statement.replace(
        f"CREATE TABLE {table}",
        f"CREATE TABLE {schema}.{table}",
        1,
    )


def _install_attached_store(
    conn: sqlite3.Connection,
    *,
    schema: str,
    spec: AssetStoreSpec,
    failpoint: Callable[[str], None] | None,
) -> None:
    stage_name = spec.asset_type
    if failpoint is not None:
        failpoint(f"before_{stage_name}_install")
    conn.execute(
        _qualified_table_ddl(
            spec.revision_ddl,
            schema=schema,
            table=spec.revision_table,
        )
    )
    conn.execute(
        _qualified_table_ddl(
            spec.head_ddl,
            schema=schema,
            table=spec.head_table,
        )
    )
    for statement in spec.index_ddl:
        index_name = statement.split()[2]
        conn.execute(
            statement.replace(
                f"CREATE INDEX {index_name}",
                f"CREATE INDEX {schema}.{index_name}",
                1,
            )
        )
    for statement in spec.trigger_ddl:
        trigger_name = statement.split()[2]
        conn.execute(
            statement.replace(
                f"CREATE TRIGGER {trigger_name}",
                f"CREATE TRIGGER {schema}.{trigger_name}",
                1,
            )
        )
    conn.execute(
        REGISTRY_DDL.replace(
            f"CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE}",
            f"CREATE TABLE IF NOT EXISTS {schema}.{REGISTRY_TABLE}",
            1,
        )
    )
    conn.execute(
        f"INSERT INTO {schema}.{REGISTRY_TABLE}"  # nosec B608
        "(component, schema_version, ddl_hash, applied_at) VALUES (?, ?, ?, ?)",
        (
            spec.schema_component,
            spec.schema_version,
            spec.ddl_hash,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    if failpoint is not None:
        failpoint(f"after_{stage_name}_schema")


def _execute_generation(
    *,
    blindspot_db: Path,
    preference_db: Path,
    actions: dict[str, str],
    failpoint: Callable[[str], None] | None,
) -> int:
    blindspot_db.parent.mkdir(parents=True, exist_ok=True)
    preference_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(blindspot_db) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("ATTACH DATABASE ? AS interaction_store", (str(preference_db),))
        conn.execute("PRAGMA interaction_store.journal_mode=DELETE")
        conn.execute("PRAGMA foreign_keys=ON")
        before_changes = conn.total_changes
        conn.execute("BEGIN IMMEDIATE")
        try:
            if (
                actions.get(USER_COGNITIVE_BLINDSPOT_SPEC.asset_type)
                == "initialize_fresh_canonical_store"
            ):
                _install_attached_store(
                    conn,
                    schema="main",
                    spec=USER_COGNITIVE_BLINDSPOT_SPEC,
                    failpoint=failpoint,
                )
            if (
                actions.get(INTERACTION_PREFERENCE_SPEC.asset_type)
                == "initialize_fresh_canonical_store"
            ):
                _install_attached_store(
                    conn,
                    schema="interaction_store",
                    spec=INTERACTION_PREFERENCE_SPEC,
                    failpoint=failpoint,
                )
            if failpoint is not None:
                failpoint("before_generation_commit")
            changed_rows = conn.total_changes - before_changes
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return changed_rows


def _recover_incomplete_generations(
    backup_root: Path,
    *,
    target_paths: tuple[Path, Path],
) -> list[str]:
    recovered: list[str] = []
    expected_targets = sorted(str(path) for path in target_paths)
    if not backup_root.is_dir():
        return recovered
    for generation_dir in sorted(backup_root.glob("user-model-assets.*")):
        manifest_path = generation_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") in {"committed", "restored", "recovered"}:
            continue
        actual_targets = sorted(str(item["path"]) for item in manifest.get("stores") or ())
        if actual_targets != expected_targets:
            continue
        _restore_pre_states(manifest)
        manifest["status"] = "recovered"
        manifest["recovered_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest_path, manifest)
        recovered.append(str(manifest.get("migration_generation") or ""))
    return recovered


def apply_plan(
    *,
    user_cognitive_blindspot_db: Path,
    interaction_preference_db: Path,
    backup_dir: Path,
    expected_plan_hash: str = "",
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
    failpoint: Callable[[str], None] | None = None,
    backup_generation: str | None = None,
) -> dict[str, Any]:
    if not expected_plan_hash:
        raise ValueError("apply requires an exact expected plan hash")
    blindspot_db = user_cognitive_blindspot_db.expanduser().resolve(strict=False)
    preference_db = interaction_preference_db.expanduser().resolve(strict=False)
    backup_root = backup_dir.expanduser().resolve(strict=False)
    common_root = Path(os.path.commonpath((str(blindspot_db.parent), str(preference_db.parent))))
    with offline_migration_lock(common_root, daemon_check=daemon_check):
        backup_root.mkdir(parents=True, exist_ok=True)
        recovered_generations = _recover_incomplete_generations(
            backup_root,
            target_paths=(blindspot_db, preference_db),
        )
        before = build_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
        )
        if before["plan_hash"] != expected_plan_hash:
            raise ValueError("expected plan hash does not match locked asset state")
        if before["refused_count"]:
            raise UserModelAssetStoreError("unknown or drifted user-model asset store is refused")
        if any(
            not store["source_integrity_ok"] or store["source_foreign_key_errors"]
            for store in before["stores"]
        ):
            raise UserModelAssetStoreError("asset store source integrity check failed")

        generation = str(backup_generation or uuid4().hex)
        generation_dir = backup_root / f"user-model-assets.{generation}"
        try:
            generation_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise RuntimeError("backup generation collision") from exc
        backups: list[dict[str, Any]] = []
        manifest_stores: list[dict[str, Any]] = []
        for store in before["stores"]:
            source_path = Path(store["path"])
            entry = {
                "path": str(source_path),
                "asset_type": store["asset_type"],
                "existed": bool(store["existed"]),
                "logical_hash": store["source_logical_hash"],
                "backup_path": "",
            }
            if store["existed"]:
                backup_path = generation_dir / f"{store['asset_type']}.sqlite"
                backup_receipt = _backup(source_path, backup_path)
                if (
                    not backup_receipt["integrity_ok"]
                    or backup_receipt["foreign_key_errors"]
                    or backup_receipt["logical_hash"] != store["source_logical_hash"]
                ):
                    raise RuntimeError("asset store backup verification failed")
                entry["backup_path"] = str(backup_path)
                backups.append(backup_receipt)
            manifest_stores.append(entry)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "prepared",
            "migration_generation": generation,
            "expected_plan_hash": expected_plan_hash,
            "stores": manifest_stores,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = generation_dir / "manifest.json"
        _write_manifest(manifest_path, manifest)
        restore_drill_ok = _restore_drill(manifest, generation_dir)
        if not restore_drill_ok:
            raise RuntimeError("asset store restore drill failed")

        actions = {store["asset_type"]: store["planned_action"] for store in before["stores"]}
        changed = any(action != "none" for action in actions.values())
        try:
            _execute_generation(
                blindspot_db=blindspot_db,
                preference_db=preference_db,
                actions=actions,
                failpoint=failpoint,
            )
            if failpoint is not None:
                failpoint("after_generation_commit")
            states = (
                read_asset_store_state(blindspot_db, USER_COGNITIVE_BLINDSPOT_SPEC),
                read_asset_store_state(preference_db, INTERACTION_PREFERENCE_SPEC),
            )
            if not all(state.ok for state in states):
                raise RuntimeError("partial user-model asset store generation")
            second_apply_changed_rows = _execute_generation(
                blindspot_db=blindspot_db,
                preference_db=preference_db,
                actions={
                    USER_COGNITIVE_BLINDSPOT_SPEC.asset_type: "none",
                    INTERACTION_PREFERENCE_SPEC.asset_type: "none",
                },
                failpoint=None,
            )
            if second_apply_changed_rows:
                raise RuntimeError("second asset migration apply changed rows")
        except BaseException:
            _restore_pre_states(manifest)
            manifest["status"] = "restored"
            manifest["restored_at"] = datetime.now(timezone.utc).isoformat()
            _write_manifest(manifest_path, manifest)
            raise

        manifest["status"] = "committed"
        manifest["committed_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest_path, manifest)
        after = build_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
        )
        return {
            **after,
            "ok": True,
            "apply": True,
            "changed": changed,
            "reviewed_plan_hash": expected_plan_hash,
            "migration_generation": generation,
            "generation_manifest": str(manifest_path),
            "backups": backups,
            "recovered_generations": recovered_generations,
            "partial_user_model_store_generation": 0,
            "asset_migration_without_plan_hash": 0,
            "backup_overwrite": 0,
            "second_apply_changed_rows": second_apply_changed_rows,
            "restore_drill_failure": 0,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-cognitive-blindspot-db", type=Path)
    parser.add_argument("--interaction-preference-db", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    blindspot_db = args.user_cognitive_blindspot_db or (
        Path(config.database_dir) / "user_cognitive_blindspots.db"
    )
    preference_db = args.interaction_preference_db or (
        Path(config.database_dir) / "interaction_preferences.db"
    )
    try:
        if args.apply:
            if args.backup_dir is None:
                raise ValueError("--apply requires --backup-dir")
            if not args.expected_plan_hash:
                raise ValueError("--apply requires --expected-plan-hash")
            report = apply_plan(
                user_cognitive_blindspot_db=blindspot_db,
                interaction_preference_db=preference_db,
                backup_dir=args.backup_dir,
                expected_plan_hash=args.expected_plan_hash,
            )
        else:
            report = build_plan(
                user_cognitive_blindspot_db=blindspot_db,
                interaction_preference_db=preference_db,
            )
    except (OSError, ValueError, sqlite3.Error, UserModelAssetStoreError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "apply": bool(args.apply),
            "ok": False,
            "error": str(exc),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    report["ok"] = not report.get("refused_count")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
