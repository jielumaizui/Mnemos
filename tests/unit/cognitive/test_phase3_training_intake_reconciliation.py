from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import core.cognitive.phase3_training_intake_reconciliation as reconciliation
from core.cognitive.phase3_training_intake_reconciliation import (
    build_phase3_training_intake_inventory,
    reconcile_phase3_training_admission_intakes,
)
from core.cognitive.training_governance import TrainingGovernanceStore
from core.cognitive.training_contract import TRAINING_ADMISSION_CONSUMER
from tests.unit.cognitive.test_training_governance_store import (
    _initialize_training_projection,
    _mature_clock,
    _objective_training_command,
)


def _remove_post_contract_intake(state_path: Path) -> None:
    with sqlite3.connect(state_path) as conn:
        row = conn.execute(
            "SELECT event_id FROM cognitive_state_outbox WHERE consumer_id=?",
            (TRAINING_ADMISSION_CONSUMER,),
        ).fetchone()
        assert row is not None
        event_id = str(row[0])
        conn.execute("DROP TRIGGER cognitive_state_outbox_no_delete")
        conn.execute(
            "DELETE FROM cognitive_state_outbox WHERE consumer_id=?",
            (TRAINING_ADMISSION_CONSUMER,),
        )
        intended = json.loads(
            str(
                conn.execute(
                    "SELECT intended_consumers FROM cognitive_data_events "
                    "WHERE event_id=?",
                    (event_id,),
                ).fetchone()[0]
            )
        )
        conn.execute("DROP TRIGGER cognitive_data_events_no_update")
        conn.execute(
            "UPDATE cognitive_data_events SET intended_consumers=? WHERE event_id=?",
            (
                json.dumps(
                    [
                        value
                        for value in intended
                        if value != TRAINING_ADMISSION_CONSUMER
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                event_id,
            ),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_outbox_no_delete
            BEFORE DELETE ON cognitive_state_outbox BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_outbox is immutable');
            END;
            CREATE TRIGGER cognitive_data_events_no_update
            BEFORE UPDATE ON cognitive_data_events BEGIN
                SELECT RAISE(ABORT, 'cognitive_data_events are immutable');
            END;
            """
        )


def test_dry_run_proposes_one_exact_pre_contract_intake(tmp_path: Path) -> None:
    state, _principal, _outcome, target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)

    inventory = build_phase3_training_intake_inventory(tmp_path)

    assert inventory["eligible_objective_attributions"] == 1
    assert inventory["proposed_count"] == 1
    assert inventory["existing_count"] == 0
    assert inventory["unresolved_count"] == 0
    candidate = inventory["objects"][0]
    assert candidate.target_command_id == target["command_id"]
    assert candidate.intake_command.consumer_id == TRAINING_ADMISSION_CONSUMER


def test_apply_backs_up_appends_once_and_replays_zero_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _principal, _outcome, target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    inventory = build_phase3_training_intake_inventory(tmp_path)
    monkeypatch.setattr(reconciliation, "_runtime_is_active", lambda: False)

    applied = reconcile_phase3_training_admission_intakes(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "backups",
    )
    replay = build_phase3_training_intake_inventory(tmp_path)

    assert applied["effect"] == {"inserted": 1, "existing": 0}
    assert Path(applied["backup_manifest"]).is_file()
    assert replay["proposed_count"] == 0
    assert replay["existing_count"] == 1
    intake = next(
        item
        for item in state.pending_commands(TRAINING_ADMISSION_CONSUMER)
        if item["payload"]["training_target_ref"]["command_id"]
        == target["command_id"]
    )
    receipt = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    ).process_admission_intake(str(intake["command_id"]))
    assert receipt.status == "committed"


@pytest.mark.parametrize("stage", ("after_event", "after_outbox", "before_commit"))
def test_apply_failpoints_rollback_without_partial_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    state, _principal, _outcome, _target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    inventory = build_phase3_training_intake_inventory(tmp_path)
    monkeypatch.setattr(reconciliation, "_runtime_is_active", lambda: False)

    def failpoint(name: str) -> None:
        if name == stage:
            raise RuntimeError(f"failpoint:{stage}")

    with pytest.raises(RuntimeError, match=f"failpoint:{stage}"):
        reconcile_phase3_training_admission_intakes(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / f"backups-{stage}",
            failpoint=failpoint,
        )

    replay = build_phase3_training_intake_inventory(tmp_path)
    assert replay["proposed_count"] == 1
    assert replay["existing_count"] == 0
    assert state.pending_commands(TRAINING_ADMISSION_CONSUMER) == []


def test_missing_target_receipt_is_classified_and_never_proposed(tmp_path: Path) -> None:
    state, _principal, _outcome, target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_delete")
        conn.execute(
            "DELETE FROM cognitive_state_effect_receipts WHERE command_id=?",
            (target["command_id"],),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_effect_receipts_no_delete
            BEFORE DELETE ON cognitive_state_effect_receipts BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
            END;
            """
        )

    inventory = build_phase3_training_intake_inventory(tmp_path)

    assert inventory["proposed_count"] == 0
    assert inventory["existing_count"] == 0
    assert inventory["unresolved_count"] == 1
    assert "receipt" in inventory["unresolved"][0]["reason"]


def test_apply_refuses_active_runtime_before_backup_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _principal, _outcome, _target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    inventory = build_phase3_training_intake_inventory(tmp_path)
    monkeypatch.setattr(reconciliation, "_runtime_is_active", lambda: True)

    with pytest.raises(RuntimeError, match="must be inactive"):
        reconcile_phase3_training_admission_intakes(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "backups-active",
        )

    assert not (tmp_path / "backups-active").exists()
    assert state.pending_commands(TRAINING_ADMISSION_CONSUMER) == []


def test_backup_failure_happens_before_any_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _principal, _outcome, _target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    inventory = build_phase3_training_intake_inventory(tmp_path)
    monkeypatch.setattr(reconciliation, "_runtime_is_active", lambda: False)

    def fail_backup(**_kwargs):
        raise OSError("backup-failure")

    monkeypatch.setattr(reconciliation, "_backup_databases", fail_backup)
    with pytest.raises(OSError, match="backup-failure"):
        reconcile_phase3_training_admission_intakes(
            database_dir=tmp_path,
            expected_inventory_hash=inventory["inventory_hash"],
            expected_object_manifest_hash=inventory["object_manifest_hash"],
            backup_dir=tmp_path / "backups-failed",
        )

    assert state.pending_commands(TRAINING_ADMISSION_CONSUMER) == []
    assert build_phase3_training_intake_inventory(tmp_path)["proposed_count"] == 1


def test_reconciliation_cli_dry_run_is_machine_readable(tmp_path: Path) -> None:
    state, _principal, _outcome, _target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)
    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "reconcile_phase3_training_admission_intakes.py"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--database-dir",
            str(tmp_path),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == (
        "mnemos.phase3_training_intake_inventory.v1"
    )
    assert payload["proposed_count"] == 1
    assert len(payload["proposed_command_ids"]) == 1
