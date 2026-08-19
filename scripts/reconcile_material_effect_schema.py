#!/usr/bin/env python3
"""Dry-run-first reconciliation for target-local material-effect schemas."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.material_effect_schema import (  # noqa: E402
    configured_material_effect_databases,
    inspect_material_effect_schema,
    reconcile_material_effect_schema,
)
from core.cognitive.state_contract import sha256_json  # noqa: E402
from core.config import get_config  # noqa: E402
from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)
from core.ops.exclusive_file_lock import exclusive_file_lock  # noqa: E402


REPORT_SCHEMA_VERSION = "mnemos.material_effect_schema_migration.v1"


def configured_target_databases(config: Any) -> tuple[Path, ...]:
    """Return the four canonical databases that own target effect journals."""

    return configured_material_effect_databases(config)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _logical_snapshot_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with _connect_read_only(path) as conn:
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def build_material_effect_schema_inventory(
    databases: Sequence[Path],
) -> dict[str, Any]:
    """Inspect every configured target read-only and return an exact hash."""

    rows: list[dict[str, Any]] = []
    for raw_path in databases:
        path = Path(raw_path).expanduser().resolve(strict=False)
        if not path.is_file():
            rows.append(
                {
                    "path": str(path),
                    "status": "not_initialized",
                    "migration_required": False,
                }
            )
            continue
        with _connect_read_only(path) as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            state = inspect_material_effect_schema(conn)
        rows.append(
            {
                "path": str(path),
                "status": "available",
                "integrity_check": integrity,
                "snapshot_hash": _logical_snapshot_hash(path),
                "file_size_bytes": int(path.stat().st_size),
                "schema": state.as_dict(),
                "migration_required": state.migration_required,
            }
        )
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "targets": rows,
    }
    return {
        **payload,
        "inventory_hash": sha256_json(payload),
        "migration_required_count": sum(
            int(bool(row.get("migration_required"))) for row in rows
        ),
        "ok": all(
            row.get("status") == "not_initialized"
            or (
                row.get("integrity_check") == "ok"
                and row.get("schema", {}).get("classification") != "unknown"
            )
            for row in rows
        ),
    }


def _backup_database(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"backup already exists: {destination}")
    with _connect_read_only(source) as src:
        with sqlite3.connect(str(destination)) as dst:
            src.backup(dst)
    with _connect_read_only(destination) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"material-effect backup integrity failed: {source}")
    return {
        "source_path": str(source),
        "backup_path": str(destination),
        "backup_sha256": _file_sha256(destination),
        "backup_snapshot_hash": _logical_snapshot_hash(destination),
        "integrity_check": integrity,
    }


def _restore_verified_backup(backup: Mapping[str, Any]) -> None:
    """Restore one backup and prove the target equals its logical preimage."""

    source = Path(str(backup["backup_path"]))
    target = Path(str(backup["source_path"]))
    with _connect_read_only(source) as src:
        with sqlite3.connect(str(target), timeout=60) as dst:
            src.backup(dst)
    with _connect_read_only(target) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"material-effect restored integrity failed: {target}")
    restored_hash = _logical_snapshot_hash(target)
    if restored_hash != str(backup["backup_snapshot_hash"]):
        raise RuntimeError(
            f"material-effect restored target differs from preimage: {target}"
        )


def _migration_lock(database_dir: Path) -> Any:
    root = Path(database_dir)

    @contextmanager
    def hold():
        """Hold both the schema-migration and daemon lifetime locks."""

        with exclusive_file_lock(
            root / ".material_effect_schema_migration.lock",
            unavailable_message="material-effect schema migration lock is held",
        ):
            with exclusive_file_lock(
                root / "daemon.pid",
                unavailable_message="Mnemos daemon started before migration lock",
            ):
                yield

    return hold()


def apply_material_effect_schema_migration(
    *,
    databases: Sequence[Path],
    database_dir: Path,
    expected_inventory_hash: str,
    backup_dir: Path,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
) -> dict[str, Any]:
    """Back up and reconcile the exact reviewed target inventory."""

    root = Path(database_dir).expanduser().resolve(strict=False)
    backups_root = Path(backup_dir).expanduser().resolve(strict=False)
    if backups_root.exists():
        raise FileExistsError("backup directory must not already exist")
    if not daemon_check(root):
        raise RuntimeError("Mnemos daemon must be conclusively stopped before apply")
    with _migration_lock(root):
        current = build_material_effect_schema_inventory(databases)
        if current["inventory_hash"] != expected_inventory_hash:
            raise RuntimeError("material-effect inventory drifted from reviewed hash")
        if not current["ok"]:
            raise RuntimeError("material-effect inventory contains an unknown schema")
        backups_root.mkdir(parents=True, exist_ok=False)
        backup_rows: list[dict[str, Any]] = []
        targets = [
            Path(row["path"])
            for row in current["targets"]
            if row["status"] == "available"
        ]
        for index, path in enumerate(targets):
            destination = backups_root / f"{index:02d}-{path.name}.sqlite3"
            backup_rows.append(_backup_database(path, destination))

        applied: list[dict[str, Any]] = []
        try:
            for path in targets:
                with sqlite3.connect(str(path), timeout=60) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    result = reconcile_material_effect_schema(conn, apply=True)
                    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                    if integrity != "ok":
                        raise RuntimeError(
                            f"post-migration integrity failed: {path}"
                        )
                    conn.commit()
                applied.append({"path": str(path), **result})

            after = build_material_effect_schema_inventory(databases)
            if not after["ok"] or after["migration_required_count"]:
                raise RuntimeError("material-effect post-migration verification failed")
            manifest = {
                "schema_version": REPORT_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database_dir": str(root),
                "before_inventory_hash": current["inventory_hash"],
                "after_inventory_hash": after["inventory_hash"],
                "backups": backup_rows,
                "targets": applied,
            }
            manifest_path = backups_root / "material_effect_schema_manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except BaseException as exc:
            restore_errors: list[str] = []
            for backup in backup_rows:
                try:
                    _restore_verified_backup(backup)
                except (OSError, RuntimeError, sqlite3.Error) as restore_exc:
                    restore_errors.append(str(restore_exc))
            if restore_errors:
                raise RuntimeError(
                    "material-effect rollback verification failed: "
                    + "; ".join(restore_errors)
                ) from exc
            raise
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "mode": "apply",
            "before": current,
            "after": after,
            "backup_dir": str(backups_root),
            "backup_manifest": str(manifest_path),
            "applied": applied,
            "ok": True,
        }


def main(argv: Sequence[str] | None = None) -> int:
    """Run dry inventory by default or exact-hash guarded apply."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--database", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-inventory-hash", default="")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    databases = tuple(Path(value) for value in args.database) or (
        configured_target_databases(config)
    )
    try:
        if args.apply:
            if not args.expected_inventory_hash or not args.backup_dir:
                raise ValueError(
                    "--apply requires --expected-inventory-hash and --backup-dir"
                )
            report = apply_material_effect_schema_migration(
                databases=databases,
                database_dir=Path(config.database_dir),
                expected_inventory_hash=args.expected_inventory_hash,
                backup_dir=Path(args.backup_dir),
            )
        else:
            report = {
                "mode": "dry-run",
                **build_material_effect_schema_inventory(databases),
            }
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry-run",
            "ok": False,
            "error": str(exc),
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
