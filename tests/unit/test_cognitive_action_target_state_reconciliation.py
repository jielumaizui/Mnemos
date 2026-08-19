from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.hephaestus import cognitive_action_state_reconcile_executor
from core.hephaestus.cognitive_action_state_reconcile_contracts import (
    RECONCILIATION_SCHEMA_SQL,
    make_batch_id,
)
from core.hephaestus.cognitive_action_effect_audit import (
    audit_cognitive_action_effects,
)
from core.hephaestus.cognitive_action_state_reconciliation import (
    CognitiveActionStateReconciliationPaths,
    apply_cognitive_action_state_reconciliation,
    build_cognitive_action_state_reconciliation_plan,
)
from core.hephaestus.cognitive_action_targets import (
    TARGET_STATE_HASH_CONTRACT_VERSION,
    CognitiveActionTargetError,
    expected_action_owned_target_state,
    validated_cognitive_action_artifact,
)
from core.hephaestus.distill_action_router import (
    DistillActionRouter,
    DistillActionRouterOptions,
)
from core.hephaestus.distill_action_store import canonical_json, sha256_json
from core.hephaestus.distill_cognitive_action_worker import (
    DistillCognitiveActionWorker,
)
from core.migrations.model_call_ledger_reconcile.runtime import (
    is_mnemos_runtime_process,
)
from tests.unit.test_distill_cognitive_actions import _claim_payload, _result


_HASHABLE_TABLES = frozenset(
    {"cognitive_action_effects", "cognitive_action_target_receipts"}
)


def _seed_observation_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database_dir = tmp_path / "db"
    wiki_dir = tmp_path / "wiki"
    database_dir.mkdir()
    wiki_dir.mkdir()
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *args, **kwargs: None,
    )
    router = DistillActionRouter(
        DistillActionRouterOptions(database_dir=database_dir, wiki_dir=wiki_dir)
    )
    result = _result(
        _claim_payload(cognitive_actions=["create_observation"]),
        database_dir=database_dir,
    )

    def create_pages(fragments):
        path = wiki_dir / "00-Inbox" / "target-state-reconciliation.md"
        path.parent.mkdir(parents=True)
        path.write_text("# target state reconciliation\n", encoding="utf-8")
        return [str(path)], [(path, fragments[0])]

    routed = router.route(result, result.fragments, create_pages)
    assert routed.errors == []
    processed = DistillCognitiveActionWorker(
        router.db_path,
        database_dir=database_dir,
        max_attempts=1,
    ).process_queued(limit=10)
    assert processed["applied"] == 1, processed
    with sqlite3.connect(router.db_path) as connection:
        effect = connection.execute(
            "SELECT * FROM cognitive_action_effects"
        ).fetchone()
    assert effect is not None
    return (
        CognitiveActionStateReconciliationPaths(database_dir=database_dir),
        router.db_path,
        str(effect[0]),
    )


def _convert_receipt_to_legacy(
    paths: CognitiveActionStateReconciliationPaths,
    action_db: Path,
    effect_id: str,
) -> tuple[str, str]:
    with sqlite3.connect(paths.observations_path) as connection:
        connection.row_factory = sqlite3.Row
        receipt = connection.execute(
            "SELECT * FROM cognitive_action_target_receipts WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        assert receipt is not None
        state = connection.execute(
            "SELECT * FROM observations WHERE id=?",
            (str(receipt["target_object_id"]),),
        ).fetchone()
        assert state is not None
        legacy_hash = sha256_json(dict(state))
        detail = json.loads(str(receipt["detail"]))
        detail.pop("target_state_hash_contract", None)
        connection.execute(
            """
            UPDATE cognitive_action_target_receipts
            SET after_hash=?, detail=?
            WHERE effect_id=?
            """,
            (legacy_hash, canonical_json(detail), effect_id),
        )
        connection.execute(
            """
            UPDATE observations
            SET access_control=?, user_notes='operator note',
                updated_at='2026-07-19T20:00:00+00:00', version=7
            WHERE id=?
            """,
            (
                canonical_json(
                    {
                        "owner": {"principal_id": "local_user", "agent": "codex"},
                        "scope": {"scope_type": "object", "scope_id": "fixture"},
                    }
                ),
                str(receipt["target_object_id"]),
            ),
        )
        receipt_hash = sha256_json(dict(receipt) | {"after_hash": legacy_hash, "detail": canonical_json(detail)})
    with sqlite3.connect(action_db) as connection:
        connection.execute(
            "UPDATE cognitive_action_effects SET after_hash=? WHERE effect_id=?",
            (legacy_hash, effect_id),
        )
    return legacy_hash, receipt_hash


def _hash_rows(path: Path, table: str) -> str:
    if table not in _HASHABLE_TABLES:
        raise ValueError("unsupported test table")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(f"SELECT * FROM {table}")  # nosec B608
        ]
    return sha256_json(rows)


def _table_exists(path: Path, table: str) -> bool:
    with sqlite3.connect(path) as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        )


def test_v3_projection_ignores_independent_fields_and_detects_action_tamper(
    tmp_path,
    monkeypatch,
):
    paths, action_db, _ = _seed_observation_action(tmp_path, monkeypatch)
    assert TARGET_STATE_HASH_CONTRACT_VERSION == "mnemos.cognitive_action_target_state.v3"
    with sqlite3.connect(paths.observations_path) as connection:
        connection.execute(
            """
            UPDATE observations
            SET confidence=0.17, base_confidence=0.23,
                base_measurement_status='historical_unverified',
                calibration_revision_id='cal-1', calibration_input_hash='input-1',
                calibration_spec_hash='spec-1', calibration_record_hash='record-1',
                source_span_ids='["span-1"]', access_control='{"owner":"local"}',
                user_notes='operator note', updated_at='2026-07-19T20:00:00+00:00',
                version=9
            """
        )

    evolved = audit_cognitive_action_effects(action_db)
    assert evolved["ok"] is True, evolved

    with sqlite3.connect(paths.observations_path) as connection:
        row = connection.execute("SELECT id, value FROM observations").fetchone()
        value = json.loads(str(row[1]))
        value["claim_text"] = "tampered action-owned state"
        connection.execute(
            "UPDATE observations SET value=? WHERE id=?",
            (canonical_json(value), str(row[0])),
        )

    tampered = audit_cognitive_action_effects(action_db)
    assert tampered["ok"] is False
    assert tampered["gaps"]["target_state_hash_mismatches"] == 1


def test_v3_projection_normalizes_equivalent_timestamp_encodings(
    tmp_path,
    monkeypatch,
):
    paths, action_db, _ = _seed_observation_action(tmp_path, monkeypatch)
    with sqlite3.connect(paths.observations_path) as connection:
        row = connection.execute(
            "SELECT observed_at, created_at FROM observations"
        ).fetchone()
        assert row is not None
        observed_at = str(row[0]).replace("+00:00", "Z")
        created_at = str(row[1]).replace("+00:00", "Z")
        connection.execute(
            "UPDATE observations SET observed_at=?, created_at=?",
            (observed_at, created_at),
        )

    assert audit_cognitive_action_effects(action_db)["ok"] is True


@pytest.mark.parametrize(
    "mutation",
    ("unit", "evidence", "observed_at", "period_start", "created_at"),
)
def test_v3_projection_rejects_every_action_owned_tamper(
    tmp_path,
    monkeypatch,
    mutation,
):
    paths, action_db, _ = _seed_observation_action(tmp_path, monkeypatch)
    statements = {
        "unit": ("UPDATE observations SET unit=?", ("milliseconds",)),
        "evidence": (
            "UPDATE observations SET evidence=?",
            (canonical_json(["tampered evidence"]),),
        ),
        "observed_at": (
            "UPDATE observations SET observed_at=?",
            ("2040-01-01T00:00:00+00:00",),
        ),
        "period_start": (
            "UPDATE observations SET period_start=?",
            ("2040-01-01T00:00:00+00:00",),
        ),
        "created_at": (
            "UPDATE observations SET created_at=?",
            ("2040-01-01T00:00:00+00:00",),
        ),
    }
    statement, parameters = statements[mutation]
    with sqlite3.connect(paths.observations_path) as connection:
        connection.execute(statement, parameters)

    audit = audit_cognitive_action_effects(action_db)

    assert audit["ok"] is False
    assert audit["gaps"]["target_state_hash_mismatches"] == 1


def test_v3_projection_rejects_invalid_timestamp(tmp_path, monkeypatch):
    paths, action_db, _ = _seed_observation_action(tmp_path, monkeypatch)
    with sqlite3.connect(paths.observations_path) as connection:
        connection.execute("UPDATE observations SET observed_at='not-a-timestamp'")

    audit = audit_cognitive_action_effects(action_db)

    assert audit["ok"] is False
    assert audit["gaps"]["target_state_hash_mismatches"] == 1


def test_artifact_timestamp_reconstruction_never_guesses_current_time(
    tmp_path,
    monkeypatch,
):
    _, action_db, _ = _seed_observation_action(tmp_path, monkeypatch)
    with sqlite3.connect(action_db) as connection:
        connection.row_factory = sqlite3.Row
        command = dict(connection.execute("SELECT * FROM cognitive_action_log").fetchone())
    artifact = json.loads(str(command["artifact_payload"]))
    artifact["created_at"] = "not-a-timestamp"
    command["artifact_payload"] = canonical_json(artifact)
    command["artifact_hash"] = sha256_json(artifact)
    validated = validated_cognitive_action_artifact(command)

    with pytest.raises(
        CognitiveActionTargetError,
        match="artifact created_at is invalid",
    ):
        expected_action_owned_target_state(
            "observation_store",
            command,
            validated,
        )


def test_exact_legacy_reconciliation_preserves_old_evidence_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    legacy_hash, _ = _convert_receipt_to_legacy(paths, action_db, effect_id)
    action_before = _hash_rows(action_db, "cognitive_action_effects")
    receipt_before = _hash_rows(
        paths.observations_path,
        "cognitive_action_target_receipts",
    )
    failed = audit_cognitive_action_effects(action_db)
    assert failed["gaps"]["target_state_hash_mismatches"] == 1

    plan = build_cognitive_action_state_reconciliation_plan(paths)
    assert plan.ok is True, plan.blocked
    assert len(plan.candidates) == 1
    assert plan.candidates[0].recorded_after_hash == legacy_hash
    monkeypatch.setattr(
        "core.hephaestus.cognitive_action_state_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    result = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "backups",
    )

    assert result["ok"] is True, result
    assert result["status"] == "verified"
    assert result["applied_count"] == 1
    assert len(result["backups"]) == 2
    assert all(item["integrity_check"] == "ok" for item in result["backups"])
    assert _hash_rows(action_db, "cognitive_action_effects") == action_before
    assert (
        _hash_rows(paths.observations_path, "cognitive_action_target_receipts")
        == receipt_before
    )
    closed = audit_cognitive_action_effects(action_db)
    assert closed["ok"] is True, closed
    assert closed["counts"]["target_state_reconciliations"] == 1

    with sqlite3.connect(paths.observations_path) as connection:
        connection.execute(
            """
            UPDATE observations
            SET user_notes='post-migration operator note', confidence=0.41,
                updated_at='2026-07-20T01:00:00+00:00', version=8
            """
        )
    independently_evolved = audit_cognitive_action_effects(action_db)
    assert independently_evolved["ok"] is True, independently_evolved

    with sqlite3.connect(paths.observations_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cognitive_action_target_state_reconciliations"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE cognitive_action_target_state_reconciliations
                SET applied_at='changed'
                """
            )

    clean = build_cognitive_action_state_reconciliation_plan(paths)
    assert clean.ok is True
    assert not clean.candidates
    replay = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=clean.inventory_hash,
        expected_object_manifest_hash=clean.object_manifest_hash,
        backup_dir=tmp_path / "unused-backups",
    )
    assert replay["status"] == "noop"
    assert replay["backups"] == []


def test_reconciled_object_still_rejects_action_owned_state_drift(
    tmp_path,
    monkeypatch,
):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    monkeypatch.setattr(
        cognitive_action_state_reconcile_executor,
        "runtime_writers_are_inactive",
        lambda _path: True,
    )
    applied = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "backups",
    )
    assert applied["ok"] is True
    with sqlite3.connect(paths.observations_path) as connection:
        connection.execute(
            "UPDATE observations SET evidence=?",
            (canonical_json(["post-migration forged evidence"]),),
        )

    audit = audit_cognitive_action_effects(action_db)

    assert audit["ok"] is False
    assert audit["gaps"]["target_state_hash_mismatches"] == 1


def test_planner_blocks_legacy_object_with_action_owned_tamper(tmp_path, monkeypatch):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    with sqlite3.connect(paths.observations_path) as connection:
        row = connection.execute("SELECT id, value FROM observations").fetchone()
        value = json.loads(str(row[1]))
        value["scope"] = {"domain": "tampered"}
        connection.execute(
            "UPDATE observations SET value=? WHERE id=?",
            (canonical_json(value), str(row[0])),
        )

    plan = build_cognitive_action_state_reconciliation_plan(paths)

    assert plan.ok is False
    assert not plan.candidates
    assert plan.blocked[0]["reason"] == "action_owned_state_mismatch"
    assert audit_cognitive_action_effects(action_db)["ok"] is False


def test_apply_blocks_runtime_or_reviewed_inventory_drift(tmp_path, monkeypatch):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    action_before = _hash_rows(action_db, "cognitive_action_effects")
    receipt_before = _hash_rows(
        paths.observations_path,
        "cognitive_action_target_receipts",
    )
    assert is_mnemos_runtime_process(
        name="python3",
        cmdline=("python3", "mnemos_daemon.py", "start"),
    )
    assert is_mnemos_runtime_process(
        name="python3",
        cmdline=("python3", "mnemos_cli.py", "mcp", "serve"),
    )
    monkeypatch.setattr(
        "core.hephaestus.cognitive_action_state_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: False,
    )

    active = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "active-backups",
    )

    assert active["status"] == "blocked"
    assert active["error"] == "mnemos_runtime_active"
    assert not (tmp_path / "active-backups").exists()
    assert _hash_rows(action_db, "cognitive_action_effects") == action_before
    assert (
        _hash_rows(paths.observations_path, "cognitive_action_target_receipts")
        == receipt_before
    )
    assert not _table_exists(
        paths.observations_path,
        "cognitive_action_target_state_reconciliations",
    )

    monkeypatch.setattr(
        "core.hephaestus.cognitive_action_state_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )
    with sqlite3.connect(paths.observations_path) as connection:
        connection.execute("UPDATE observations SET user_notes='drift after review'")
    drifted = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "drift-backups",
    )
    assert drifted["status"] == "blocked"
    assert drifted["error"] == "inventory_hash_mismatch"
    assert not (tmp_path / "drift-backups").exists()


def test_apply_failure_restores_candidate_inventory(tmp_path, monkeypatch):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    monkeypatch.setattr(
        "core.hephaestus.cognitive_action_state_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )

    def failpoint(stage: str) -> None:
        if stage == "after_reconciliation_insert":
            raise RuntimeError("injected failure")

    result = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "backups",
        failpoint=failpoint,
    )

    assert result["status"] == "rolled_back"
    assert result["rollback_verified"] is True
    restored = build_cognitive_action_state_reconciliation_plan(paths)
    assert restored.inventory_hash == plan.inventory_hash
    assert audit_cognitive_action_effects(action_db)["ok"] is False


def test_backup_failure_precedes_every_reconciliation_write(tmp_path, monkeypatch):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    action_before = _hash_rows(action_db, "cognitive_action_effects")
    receipt_before = _hash_rows(
        paths.observations_path,
        "cognitive_action_target_receipts",
    )
    monkeypatch.setattr(
        cognitive_action_state_reconcile_executor,
        "runtime_writers_are_inactive",
        lambda _path: True,
    )

    def fail_backup(*_args, **_kwargs):
        raise OSError("injected backup failure")

    monkeypatch.setattr(
        cognitive_action_state_reconcile_executor,
        "backup_sqlite_databases",
        fail_backup,
    )

    with pytest.raises(OSError, match="injected backup failure"):
        apply_cognitive_action_state_reconciliation(
            paths,
            expected_inventory_hash=plan.inventory_hash,
            expected_object_manifest_hash=plan.object_manifest_hash,
            backup_dir=tmp_path / "backups",
        )

    assert _hash_rows(action_db, "cognitive_action_effects") == action_before
    assert (
        _hash_rows(paths.observations_path, "cognitive_action_target_receipts")
        == receipt_before
    )
    assert not _table_exists(
        paths.observations_path,
        "cognitive_action_target_state_reconciliations",
    )


def test_apply_blocks_inventory_drift_after_verified_backup(tmp_path, monkeypatch):
    paths, _, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, paths.action_path, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    monkeypatch.setattr(
        cognitive_action_state_reconcile_executor,
        "runtime_writers_are_inactive",
        lambda _path: True,
    )
    original_backup = (
        cognitive_action_state_reconcile_executor.backup_sqlite_databases
    )

    def drift_after_backup(sources, backup_dir, *, label):
        receipts = original_backup(sources, backup_dir, label=label)
        with sqlite3.connect(paths.observations_path) as connection:
            connection.execute(
                "UPDATE observations SET user_notes='post-backup inventory drift'"
            )
        return receipts

    monkeypatch.setattr(
        cognitive_action_state_reconcile_executor,
        "backup_sqlite_databases",
        drift_after_backup,
    )

    result = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "backups",
    )

    assert result["ok"] is False
    assert result["error"] == "inventory_drift_after_backup"
    assert len(result["backups"]) == 2
    assert not _table_exists(
        paths.observations_path,
        "cognitive_action_target_state_reconciliations",
    )


def test_sqlite_failure_restores_candidate_inventory(tmp_path, monkeypatch):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    monkeypatch.setattr(
        cognitive_action_state_reconcile_executor,
        "runtime_writers_are_inactive",
        lambda _path: True,
    )

    def failpoint(stage: str) -> None:
        if stage == "after_reconciliation_insert":
            raise sqlite3.OperationalError("injected sqlite failure")

    result = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "backups",
        failpoint=failpoint,
    )

    assert result["status"] == "rolled_back"
    assert result["rollback_verified"] is True
    assert "OperationalError" in result["error"]
    restored = build_cognitive_action_state_reconciliation_plan(paths)
    assert restored.inventory_hash == plan.inventory_hash


def test_audit_recomputes_inventory_manifest_source_bindings(tmp_path, monkeypatch):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    monkeypatch.setattr(
        cognitive_action_state_reconcile_executor,
        "runtime_writers_are_inactive",
        lambda _path: True,
    )
    applied = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "backups",
    )
    assert applied["ok"] is True

    with sqlite3.connect(paths.observations_path) as connection:
        connection.row_factory = sqlite3.Row
        batch = dict(
            connection.execute(
                "SELECT * FROM cognitive_action_target_state_reconciliation_batches"
            ).fetchone()
        )
        payload = json.loads(str(batch["inventory_manifest_json"]))
        forged_command_hash = sha256_json({"forged": "command"})
        payload["candidates"][0]["command_hash"] = forged_command_hash
        payload["inventory"][0]["command_hash"] = forged_command_hash
        object_manifest = payload["candidates"]
        object_manifest_hash = sha256_json(object_manifest)
        inventory_hash = sha256_json(payload)
        batch_id = make_batch_id(inventory_hash, object_manifest_hash)
        connection.execute(
            "DROP TRIGGER trg_cognitive_action_state_reconciliation_no_update"
        )
        connection.execute(
            "DROP TRIGGER trg_cognitive_action_state_reconcile_batch_no_update"
        )
        connection.execute(
            """
            UPDATE cognitive_action_target_state_reconciliation_batches
            SET batch_id=?, inventory_hash=?, object_manifest_hash=?,
                inventory_manifest_json=?, object_manifest_json=?
            """,
            (
                batch_id,
                inventory_hash,
                object_manifest_hash,
                canonical_json(payload),
                canonical_json(object_manifest),
            ),
        )
        connection.execute(
            """
            UPDATE cognitive_action_target_state_reconciliations
            SET batch_id=?, command_hash=?, inventory_hash=?,
                object_manifest_hash=?
            """,
            (
                batch_id,
                forged_command_hash,
                inventory_hash,
                object_manifest_hash,
            ),
        )
        connection.executescript(RECONCILIATION_SCHEMA_SQL)

    audit = audit_cognitive_action_effects(action_db)

    assert audit["ok"] is False
    assert audit["gaps"]["target_state_reconciliation_schema_gaps"] == 0
    assert audit["gaps"]["invalid_target_state_reconciliations"] == 1


def test_planner_rejects_check_text_hidden_in_a_default(
    tmp_path,
    monkeypatch,
):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    weakened_schema = RECONCILIATION_SCHEMA_SQL.replace(
        "target TEXT NOT NULL CHECK(target='observation_store'),",
        'target TEXT NOT NULL DEFAULT "CHECK(target=\'observation_store\')",',
    )
    with sqlite3.connect(paths.observations_path) as connection:
        connection.executescript(weakened_schema)

    plan = build_cognitive_action_state_reconciliation_plan(paths)

    assert plan.ok is False
    assert plan.blocked[0]["reason"] == "target_state_reconciliation_schema_drift"


def test_planner_rejects_same_column_schema_without_unique_contracts(
    tmp_path,
    monkeypatch,
):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    weakened_schema = RECONCILIATION_SCHEMA_SQL.replace(" UNIQUE", "")
    with sqlite3.connect(paths.observations_path) as connection:
        connection.executescript(weakened_schema)

    plan = build_cognitive_action_state_reconciliation_plan(paths)

    assert plan.ok is False
    assert plan.blocked[0]["reason"] == "target_state_reconciliation_schema_drift"


def test_audit_rejects_reconciliation_schema_drift(tmp_path, monkeypatch):
    paths, action_db, effect_id = _seed_observation_action(tmp_path, monkeypatch)
    _convert_receipt_to_legacy(paths, action_db, effect_id)
    plan = build_cognitive_action_state_reconciliation_plan(paths)
    monkeypatch.setattr(
        "core.hephaestus.cognitive_action_state_reconcile_executor.runtime_writers_are_inactive",
        lambda _path: True,
    )
    applied = apply_cognitive_action_state_reconciliation(
        paths,
        expected_inventory_hash=plan.inventory_hash,
        expected_object_manifest_hash=plan.object_manifest_hash,
        backup_dir=tmp_path / "backups",
    )
    assert applied["ok"] is True
    with sqlite3.connect(paths.observations_path) as connection:
        connection.execute(
            "DROP TRIGGER trg_cognitive_action_state_reconciliation_no_update"
        )
        connection.execute(
            """
            CREATE TRIGGER trg_cognitive_action_state_reconciliation_no_update
            BEFORE UPDATE ON cognitive_action_target_state_reconciliations
            BEGIN
                SELECT iif(
                    0,
                    RAISE(ABORT, 'dormant immutable expression'),
                    0
                );
            END
            """
        )

    audit = audit_cognitive_action_effects(action_db)

    assert audit["ok"] is False
    assert audit["gaps"]["target_state_reconciliation_schema_gaps"] == 1
    drifted = build_cognitive_action_state_reconciliation_plan(paths)
    assert drifted.ok is False
    assert drifted.blocked[0]["reason"] == "target_state_reconciliation_schema_drift"
