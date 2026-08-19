from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import sqlite3

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "reconcile_profile_assertion_revisions.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("reconcile_profile_revisions", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reconcile_profile_assertion_revisions_dry_run_then_apply(tmp_path):
    from core.persona.psyche import SignalStore

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(db_path=db_path, initialize_schema=True)
    store.close()
    # Simulate the pre-COG-019 production schema without mutating it through
    # the reconcile dry-run itself.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE profile_assertion_revisions")
        conn.execute("ALTER TABLE profile_assertions DROP COLUMN current_revision_id")
        conn.execute("DROP TABLE profile_read_authorizations")
        conn.execute("DROP TABLE profile_usage_outbox")
        conn.execute("ALTER TABLE profile_usage_log DROP COLUMN read_authorization_token")
        conn.execute("ALTER TABLE profile_usage_log DROP COLUMN target_receipt")
        conn.execute(
            """
            INSERT INTO profile_assertions (
                assertion_id, dimension, claim, supporting_signals,
                contradicting_signals, confidence, privacy_level,
                last_verified_at, revision_policy, status, access_control
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pa_legacy_anchor",
                "judgment_standard",
                "legacy assertion must be retained",
                '["profile_signals:1"]',
                "[]",
                0.8,
                "local",
                "2026-07-23T00:00:00",
                "revise_on_contradiction",
                "active",
                "",
            ),
        )

    module = _module()
    planned = module.inspect(db_path)
    assert planned["read_only"] is True
    assert planned["revision_table_exists"] is False
    assert planned["missing_history_count"] == 1
    assert planned["partial_profile_schema_migration"] == 1
    assert planned["plan_hash"].startswith("sha256:")
    assert planned["ok"] is False

    with pytest.raises(ValueError, match="expected plan hash"):
        module.apply(db_path, tmp_path / "backup")
    result = module.apply(
        db_path,
        tmp_path / "backup",
        expected_plan_hash=planned["plan_hash"],
        daemon_check=lambda _database_dir: True,
    )
    assert result["ok"] is True
    assert result["inserted_revision_count"] == 1
    assert result["head_gap_count"] == 0
    assert result["projection_head_mismatch"] == 0
    assert result["schema_errors"] == []
    assert Path(result["backup_path"]).is_file()
    assert result["reviewed_plan_hash"] == planned["plan_hash"]
    assert result["source_integrity_ok"] is True
    assert result["backup_integrity_ok"] is True
    assert result["restore_drill_ok"] is True
    assert result["second_apply_changed_rows"] == 0

    healthy_plan = module.inspect(db_path)
    repeated = module.apply(
        db_path,
        tmp_path / "backup",
        expected_plan_hash=healthy_plan["plan_hash"],
        daemon_check=lambda _database_dir: True,
    )
    assert repeated["ok"] is True
    assert repeated["inserted_revision_count"] == 0
    assert repeated["second_apply_changed_rows"] == 0


def test_signal_store_default_open_is_schema_read_only_and_fails_closed(tmp_path):
    from core.persona.psyche import SignalStore

    missing = tmp_path / "missing" / "user_signals.db"
    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="explicit"):
            SignalStore(db_path=missing)
    assert not missing.exists()
    assert not missing.parent.exists()

    db_path = tmp_path / "initialized.db"
    initialized = SignalStore(db_path=db_path, initialize_schema=True)
    initialized.close()
    with sqlite3.connect(db_path) as conn:
        before = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    reopened = SignalStore(db_path=db_path)
    reopened.close()
    with sqlite3.connect(db_path) as conn:
        after = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        conn.execute("DROP TABLE profile_assertion_revisions")
        drifted_before_open = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after == before
    with pytest.raises(RuntimeError, match="reconciliation"):
        SignalStore(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        drifted_after_open = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert drifted_after_open == drifted_before_open


def test_profile_reconcile_blocks_active_writer_and_rolls_back_fault(tmp_path):
    from core.persona.psyche import SignalStore

    module = _module()
    db_path = tmp_path / "user_signals.db"
    store = SignalStore(db_path=db_path, initialize_schema=True)
    store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE profile_assertion_revisions")
        conn.execute("ALTER TABLE profile_assertions DROP COLUMN current_revision_id")
    plan = module.inspect(db_path)

    with pytest.raises(ValueError, match="does not match"):
        module.apply(
            db_path,
            tmp_path / "wrong-plan-backup",
            expected_plan_hash="sha256:" + ("0" * 64),
            daemon_check=lambda _database_dir: True,
        )
    with pytest.raises(RuntimeError, match="stopped"):
        module.apply(
            db_path,
            tmp_path / "blocked-backup",
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: False,
        )

    before = db_path.read_bytes()
    with pytest.raises(RuntimeError, match="injected"):
        module.apply(
            db_path,
            tmp_path / "fault-backup",
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            failpoint=lambda stage: (
                (_ for _ in ()).throw(RuntimeError("injected migration failure"))
                if stage == "after_schema_install"
                else None
            ),
        )
    assert db_path.read_bytes() == before
    after = module.inspect(db_path)
    assert after["plan_hash"] == plan["plan_hash"]

    collision_root = tmp_path / "collision-backup"
    collision_root.mkdir()
    collision_generation = "fixed-generation"
    collision_path = (
        collision_root / f"{db_path.name}.before-profile-v2.{collision_generation}.sqlite"
    )
    collision_path.write_bytes(b"existing generation")
    with pytest.raises(RuntimeError, match="collision"):
        module.apply(
            db_path,
            collision_root,
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            backup_generation=collision_generation,
        )
    assert collision_path.read_bytes() == b"existing generation"


def test_every_profile_migration_statement_stage_rolls_back(tmp_path):
    from core.persona.psyche import SignalStore

    module = _module()
    template = tmp_path / "template.db"
    SignalStore(db_path=template, initialize_schema=True).close()
    with sqlite3.connect(template) as conn:
        conn.execute("DROP TABLE profile_assertion_revisions")
        conn.execute("ALTER TABLE profile_assertions DROP COLUMN current_revision_id")
        conn.execute("ALTER TABLE profile_usage_log DROP COLUMN read_authorization_token")
        conn.execute(
            """
            INSERT INTO profile_assertions (
                assertion_id, dimension, claim, supporting_signals,
                contradicting_signals, confidence, privacy_level,
                last_verified_at, revision_policy, status, access_control
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pa_fault_matrix",
                "judgment_standard",
                "every migration statement must roll back",
                "[]",
                "[]",
                0.8,
                "local",
                "2026-07-23T00:00:00",
                "revise_on_contradiction",
                "active",
                "",
            ),
        )

    discovery = tmp_path / "discovery.db"
    shutil.copy2(template, discovery)
    stages: list[str] = []
    with sqlite3.connect(discovery) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        module._reconcile_once(conn, failpoint=stages.append)
        conn.rollback()
    assert len(stages) == len(set(stages))
    assert len(stages) >= 20

    for index, target_stage in enumerate(stages):
        candidate = tmp_path / f"fault-{index}.db"
        shutil.copy2(template, candidate)
        before_hash = module.inspect(candidate)["source_logical_hash"]
        observed_count = 0

        def fail_at_stage(stage: str) -> None:
            nonlocal observed_count
            observed_count += 1
            if observed_count == index + 1:
                raise RuntimeError(f"injected statement failure:{stage}")

        caught = False
        with sqlite3.connect(candidate) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                module._reconcile_once(conn, failpoint=fail_at_stage)
            except RuntimeError as exc:
                assert "injected statement failure" in str(exc)
                caught = True
            conn.rollback()
        assert caught, f"fault stage was not reached: {index}:{target_stage}"
        assert module.inspect(candidate)["source_logical_hash"] == before_hash
