from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.cognitive.material_effect_schema import inspect_material_effect_schema
from scripts.reconcile_material_effect_schema import (
    apply_material_effect_schema_migration,
    build_material_effect_schema_inventory,
)


def _legacy_target(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE domain_rows (id TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO domain_rows VALUES ('one', 'before')")


def test_dry_run_is_read_only_and_apply_is_backup_guarded(tmp_path: Path) -> None:
    target = tmp_path / "db" / "policy_patches.db"
    _legacy_target(target)
    before_bytes = target.read_bytes()

    inventory = build_material_effect_schema_inventory((target,))

    assert inventory["ok"] is True
    assert inventory["migration_required_count"] == 1
    assert target.read_bytes() == before_bytes

    backup_dir = tmp_path / "backup-generation-1"
    applied = apply_material_effect_schema_migration(
        databases=(target,),
        database_dir=target.parent,
        expected_inventory_hash=inventory["inventory_hash"],
        backup_dir=backup_dir,
        daemon_check=lambda _path: True,
    )

    assert applied["ok"] is True
    assert Path(applied["backup_manifest"]).is_file()
    assert applied["after"]["migration_required_count"] == 0
    with sqlite3.connect(target) as conn:
        assert inspect_material_effect_schema(conn).ok is True
        assert conn.execute("SELECT value FROM domain_rows").fetchone() == ("before",)

    replay_inventory = build_material_effect_schema_inventory((target,))
    replay = apply_material_effect_schema_migration(
        databases=(target,),
        database_dir=target.parent,
        expected_inventory_hash=replay_inventory["inventory_hash"],
        backup_dir=tmp_path / "backup-generation-2",
        daemon_check=lambda _path: True,
    )
    assert replay["ok"] is True
    assert all(not row["applied"] for row in replay["applied"])


def test_apply_rejects_source_drift_and_existing_backup_dir(tmp_path: Path) -> None:
    target = tmp_path / "db" / "user_signals.db"
    _legacy_target(target)
    inventory = build_material_effect_schema_inventory((target,))
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO domain_rows VALUES ('two', 'drift')")

    with pytest.raises(RuntimeError, match="inventory drifted"):
        apply_material_effect_schema_migration(
            databases=(target,),
            database_dir=target.parent,
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=tmp_path / "unused-backup",
            daemon_check=lambda _path: True,
        )

    existing = tmp_path / "existing-backup"
    existing.mkdir()
    current = build_material_effect_schema_inventory((target,))
    with pytest.raises(FileExistsError, match="must not already exist"):
        apply_material_effect_schema_migration(
            databases=(target,),
            database_dir=target.parent,
            expected_inventory_hash=current["inventory_hash"],
            backup_dir=existing,
            daemon_check=lambda _path: True,
        )


def test_apply_requires_conclusively_stopped_daemon(tmp_path: Path) -> None:
    target = tmp_path / "db" / "cognitive_graph.db"
    _legacy_target(target)
    inventory = build_material_effect_schema_inventory((target,))

    with pytest.raises(RuntimeError, match="daemon"):
        apply_material_effect_schema_migration(
            databases=(target,),
            database_dir=target.parent,
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=tmp_path / "backup",
            daemon_check=lambda _path: False,
        )


def test_postcheck_failure_restores_every_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.reconcile_material_effect_schema as migration

    target = tmp_path / "db" / "knowledge_graph.db"
    _legacy_target(target)
    before_snapshot = migration._logical_snapshot_hash(target)
    inventory = build_material_effect_schema_inventory((target,))
    real_builder = migration.build_material_effect_schema_inventory
    calls = 0

    def fail_postcheck(databases):
        nonlocal calls
        calls += 1
        report = real_builder(databases)
        if calls == 2:
            return {**report, "ok": False}
        return report

    monkeypatch.setattr(
        migration,
        "build_material_effect_schema_inventory",
        fail_postcheck,
    )
    with pytest.raises(RuntimeError, match="post-migration verification"):
        apply_material_effect_schema_migration(
            databases=(target,),
            database_dir=target.parent,
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=tmp_path / "failed-generation",
            daemon_check=lambda _path: True,
        )

    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT value FROM domain_rows").fetchone() == ("before",)
        assert inspect_material_effect_schema(conn).classification == "absent"
    assert migration._logical_snapshot_hash(target) == before_snapshot
