"""Object-level prediction history migration and restore tests."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core.cognitive.prediction_history_migration import (
    REASON_CODE,
    apply_prediction_history_migration,
    build_prediction_history_inventory,
    inspect_prediction_history_coverage,
    inspect_prediction_target,
    restore_prediction_backup,
)
from core.cognitive.state_schema import (
    PREDICTION_ENFORCEMENT_COMPONENT,
    initialize_cognitive_state_schema,
)


def _delivery_db(path: Path, count: int = 5) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                channel TEXT NOT NULL,
                decision TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        for index in range(count):
            conn.execute(
                "INSERT INTO delivery_events VALUES (?, ?, ?, ?, ?)",
                (
                    f"delivery-{index}",
                    f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    "predictive_push",
                    "deliver" if index < 3 else "suppress",
                    "{}",
                ),
            )
        conn.execute(
            "INSERT INTO delivery_events VALUES (?, ?, ?, ?, ?)",
            (
                "delivery-unrelated",
                "2026-07-10T00:00:00+00:00",
                "recap",
                "deliver",
                "{}",
            ),
        )
    return path


def _legacy_target(path: Path) -> Path:
    initialize_cognitive_state_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM mnemos_schema_registry WHERE component=?",
            (PREDICTION_ENFORCEMENT_COMPONENT,),
        )
    return path


def test_prediction_history_apply_restore_reapply_and_zero_change_replay(
    tmp_path: Path,
) -> None:
    delivery = _delivery_db(tmp_path / "delivery_events.db")
    target = _legacy_target(tmp_path / "producer_consumer_ledger.db")
    inventory = build_prediction_history_inventory(delivery)

    assert len(inventory.objects) == 5
    assert {
        item.decision for item in inventory.objects
    } == {"deliver", "suppress"}
    applied = apply_prediction_history_migration(
        delivery_db=delivery,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backup-one",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert applied["inserted"] == 5
    assert applied["existing"] == 0
    assert applied["after"]["activation_marker"] is True
    assert applied["after"]["active_prediction_revision_count"] == 0
    coverage = inspect_prediction_history_coverage(delivery, target)
    assert coverage["ok"] is True
    assert coverage["historical_predictive_object_uncovered"] == 0

    restored = restore_prediction_backup(
        target_db=target,
        restore_manifest=Path(applied["backup"]["restore_manifest"]),
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )
    assert restored["ok"] is True
    assert inspect_prediction_target(target)["activation_marker"] is False

    reapplied = apply_prediction_history_migration(
        delivery_db=delivery,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backup-two",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )
    replay = apply_prediction_history_migration(
        delivery_db=delivery,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backup-three",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert reapplied["inserted"] == 5
    assert replay["inserted"] == 0
    assert replay["existing"] == 5
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_migration_quarantine "
            "WHERE reason_code=?",
            (REASON_CODE,),
        ).fetchone() == (5,)
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions "
            "WHERE object_type='prediction_record'"
        ).fetchone() == (0,)


def test_prediction_history_rejects_source_drift_before_apply(tmp_path: Path) -> None:
    delivery = _delivery_db(tmp_path / "delivery_events.db", count=1)
    target = _legacy_target(tmp_path / "producer_consumer_ledger.db")
    inventory = build_prediction_history_inventory(delivery)
    with sqlite3.connect(delivery) as conn:
        conn.execute(
            "INSERT INTO delivery_events VALUES (?, ?, ?, ?, ?)",
            (
                "delivery-drift",
                "2026-07-12T00:00:00+00:00",
                "predictive_push",
                "deliver",
                "{}",
            ),
        )

    with pytest.raises(RuntimeError, match="drifted before apply"):
        apply_prediction_history_migration(
            delivery_db=delivery,
            target_db=target,
            expected_inventory_hash=inventory.inventory_hash,
            backup_dir=tmp_path / "backup",
            database_dir=tmp_path,
            daemon_check=lambda _: True,
        )
    assert not (tmp_path / "backup").exists()


def test_prediction_history_apply_rolls_back_before_commit(tmp_path: Path) -> None:
    delivery = _delivery_db(tmp_path / "delivery_events.db", count=1)
    target = _legacy_target(tmp_path / "producer_consumer_ledger.db")
    inventory = build_prediction_history_inventory(delivery)

    def failpoint(name: str) -> None:
        if name == "before_commit":
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        apply_prediction_history_migration(
            delivery_db=delivery,
            target_db=target,
            expected_inventory_hash=inventory.inventory_hash,
            backup_dir=tmp_path / "backup",
            database_dir=tmp_path,
            daemon_check=lambda _: True,
            failpoint=failpoint,
        )

    target_state = inspect_prediction_target(target)
    assert target_state["activation_marker"] is False
    assert target_state["historical_quarantine_count"] == 0
