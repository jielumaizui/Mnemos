"""Explicit initialization planner for independent COG-016 stores."""

from __future__ import annotations

import sqlite3

import pytest

from core.cognitive.user_model_asset_store import (
    USER_COGNITIVE_BLINDSPOT_SPEC,
    UserModelAssetStoreError,
    initialize_asset_store,
)
from scripts.reconcile_user_model_asset_stores import apply_plan, build_plan


def test_dry_run_is_read_only_for_absent_stores(tmp_path):
    blindspot_db = tmp_path / "runtime" / "blindspots.db"
    preference_db = tmp_path / "runtime" / "preferences.db"

    report = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )

    assert report["refused_count"] == 0
    assert {item["planned_action"] for item in report["stores"]} == {
        "initialize_fresh_canonical_store"
    }
    assert report["legacy_active_promotion_count"] == 0
    assert report["plan_hash"].startswith("sha256:")
    assert report["asset_migration_without_plan_hash"] == 0
    assert not blindspot_db.parent.exists()


def test_apply_initializes_separate_stores_and_followup_is_noop(tmp_path):
    blindspot_db = tmp_path / "runtime" / "blindspots.db"
    preference_db = tmp_path / "runtime" / "preferences.db"
    backup_dir = tmp_path / "backups"

    plan = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )
    with pytest.raises(ValueError, match="expected plan hash"):
        apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=backup_dir,
        )
    report = apply_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
        backup_dir=backup_dir,
        expected_plan_hash=plan["plan_hash"],
        daemon_check=lambda _database_dir: True,
    )
    followup = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )

    assert report["changed"] is True
    assert report["legacy_active_promotion_count"] == 0
    assert report["partial_user_model_store_generation"] == 0
    assert report["second_apply_changed_rows"] == 0
    assert report["restore_drill_failure"] == 0
    assert report["backup_overwrite"] == 0
    assert blindspot_db.is_file() and preference_db.is_file()
    assert {item["planned_action"] for item in followup["stores"]} == {"none"}
    repeated = apply_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
        backup_dir=backup_dir,
        expected_plan_hash=followup["plan_hash"],
        daemon_check=lambda _database_dir: True,
    )
    assert repeated["changed"] is False
    assert repeated["second_apply_changed_rows"] == 0


def test_apply_refuses_unknown_existing_schema(tmp_path):
    blindspot_db = tmp_path / "blindspots.db"
    preference_db = tmp_path / "preferences.db"
    with sqlite3.connect(blindspot_db) as conn:
        conn.execute("CREATE TABLE user_cognitive_blindspot_revisions(id TEXT)")

    with pytest.raises(UserModelAssetStoreError, match="unknown or drifted"):
        apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=tmp_path / "backups",
            expected_plan_hash=build_plan(
                user_cognitive_blindspot_db=blindspot_db,
                interaction_preference_db=preference_db,
            )["plan_hash"],
            daemon_check=lambda _database_dir: True,
        )


@pytest.mark.parametrize(
    "failure_stage",
    (
        "before_interaction_preference_install",
        "after_interaction_preference_schema",
        "before_generation_commit",
    ),
)
def test_second_store_failure_restores_both_pre_states(tmp_path, failure_stage):
    blindspot_db = tmp_path / "runtime" / "blindspots.db"
    preference_db = tmp_path / "runtime" / "preferences.db"
    plan = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )

    def failpoint(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected asset migration failure:{stage}")

    with pytest.raises(RuntimeError, match="injected asset migration failure"):
        apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=tmp_path / "backups",
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            failpoint=failpoint,
        )
    assert not blindspot_db.exists()
    assert not preference_db.exists()


def test_asset_migration_blocks_writer_and_backup_generation_collision(tmp_path):
    blindspot_db = tmp_path / "runtime" / "blindspots.db"
    preference_db = tmp_path / "runtime" / "preferences.db"
    backup_dir = tmp_path / "backups"
    plan = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )
    with pytest.raises(ValueError, match="does not match"):
        apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=backup_dir,
            expected_plan_hash="sha256:" + ("0" * 64),
            daemon_check=lambda _database_dir: True,
        )
    with pytest.raises(RuntimeError, match="stopped"):
        apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=backup_dir,
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: False,
        )
    collision_generation = "fixed-generation"
    (backup_dir / f"user-model-assets.{collision_generation}").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="collision"):
        apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=backup_dir,
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            backup_generation=collision_generation,
        )
    assert not blindspot_db.exists()
    assert not preference_db.exists()


def test_existing_first_store_is_restored_when_second_store_fails(tmp_path):
    blindspot_db = tmp_path / "runtime" / "blindspots.db"
    preference_db = tmp_path / "runtime" / "preferences.db"
    initialize_asset_store(blindspot_db, USER_COGNITIVE_BLINDSPOT_SPEC)
    plan = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )
    before_hash = plan["stores"][0]["source_logical_hash"]

    def failpoint(stage: str) -> None:
        if stage == "after_interaction_preference_schema":
            raise RuntimeError("injected asset migration failure")

    with pytest.raises(RuntimeError, match="injected asset migration failure"):
        apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=tmp_path / "backups",
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            failpoint=failpoint,
        )
    restored = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )
    assert restored["stores"][0]["source_logical_hash"] == before_hash
    assert restored["stores"][0]["before"]["ok"] is True
    assert not preference_db.exists()


def test_prepared_generation_recovers_after_process_death_window(tmp_path, monkeypatch):
    import scripts.reconcile_user_model_asset_stores as module

    blindspot_db = tmp_path / "runtime" / "blindspots.db"
    preference_db = tmp_path / "runtime" / "preferences.db"
    backup_dir = tmp_path / "backups"
    plan = build_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
    )

    def fail_after_commit(stage: str) -> None:
        if stage == "after_generation_commit":
            raise RuntimeError("simulated process death")

    original_restore = module._restore_pre_states
    monkeypatch.setattr(
        module,
        "_restore_pre_states",
        lambda _manifest: (_ for _ in ()).throw(RuntimeError("restore interrupted")),
    )
    with pytest.raises(RuntimeError, match="restore interrupted"):
        module.apply_plan(
            user_cognitive_blindspot_db=blindspot_db,
            interaction_preference_db=preference_db,
            backup_dir=backup_dir,
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            failpoint=fail_after_commit,
        )
    assert blindspot_db.exists() and preference_db.exists()

    monkeypatch.setattr(module, "_restore_pre_states", original_restore)
    recovered = module.apply_plan(
        user_cognitive_blindspot_db=blindspot_db,
        interaction_preference_db=preference_db,
        backup_dir=backup_dir,
        expected_plan_hash=plan["plan_hash"],
        daemon_check=lambda _database_dir: True,
    )
    assert recovered["ok"] is True
    assert recovered["recovered_generations"]
    assert recovered["partial_user_model_store_generation"] == 0
