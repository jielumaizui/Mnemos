"""Apply an exact, reviewed calibration provenance reconciliation plan."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from core.cognitive.auto_calibration import CalibrationEngine
from core.cognitive.calibration_record import CalibrationRecordStore
from core.cognitive.calibration_reconcile_backup import (
    backup_projection_files,
    backup_sqlite_databases,
    restore_backups,
)
from core.cognitive.calibration_reconcile_contracts import (
    CalibrationReconciliationPaths,
    CalibrationReconciliationPlan,
    MIGRATION_ID,
)
from core.cognitive.calibration_reconcile_planner import (
    build_calibration_reconciliation_plan,
)
from core.cognitive.observation_engine import ObservationEngine
from core.cognitive.observation_store import ObservationStore
from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive
from core.migrations.registry import MigrationLedger, MigrationLedgerRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_hash(row: sqlite3.Row) -> str:
    return sha256_json(
        {
            str(key): row[key]
            for key in row.keys()
            if str(key) != "access_control"
        }
    )


def _verify_integrity(paths: CalibrationReconciliationPaths) -> dict[str, str]:
    results: dict[str, str] = {}
    for path in (
        paths.state_path,
        paths.observations_path,
        paths.raw_path,
        paths.migrations_path,
    ):
        if not path.is_file():
            continue
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            results[path.name] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if any(value != "ok" for value in results.values()):
        raise RuntimeError("post-apply SQLite integrity check failed")
    return results


def _retire_collision(
    paths: CalibrationReconciliationPaths,
    retirement: Any,
) -> None:
    with sqlite3.connect(paths.state_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT h.revision_id, r.payload_hash
            FROM cognitive_state_heads AS h
            JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
            WHERE h.object_type='calibration_record' AND h.object_id=?
            """,
            (retirement.object_id,),
        ).fetchone()
        if row is None or (
            str(row["revision_id"]) != retirement.old_revision_id
            or str(row["payload_hash"]) != retirement.old_payload_hash
        ):
            raise RuntimeError("collision retirement state precondition drifted")
        quarantine_payload = {
            "schema_version": "mnemos.calibration_collision_retirement.v1",
            **retirement.manifest(),
        }
        quarantine_hash = sha256_json(quarantine_payload)
        quarantine_id = (
            "calibration-retirement-quarantine-"
            + quarantine_hash.split(":", 1)[1][:32]
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_migration_quarantine (
                quarantine_id, source_table, source_key, reason_code,
                field_manifest, payload_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quarantine_id,
                "cognitive_state_revisions",
                retirement.old_revision_id,
                "retired_legacy_system_identity_collision",
                canonical_json(sorted(quarantine_payload)),
                canonical_json(quarantine_payload),
                quarantine_hash,
                _now(),
            ),
        )
        conn.execute(
            "DELETE FROM cognitive_state_heads WHERE object_type=? AND object_id=?",
            ("calibration_record", retirement.object_id),
        )

    with sqlite3.connect(paths.observations_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM observations WHERE id=?",
            (retirement.object_id,),
        ).fetchone()
        if row is None or _row_hash(row) != retirement.observation_row_hash:
            raise RuntimeError("collision retirement Observation precondition drifted")
        conn.execute("DELETE FROM observations WHERE id=?", (retirement.object_id,))


def _record_observation_effect(
    records: CalibrationRecordStore,
    receipt: Any,
    report: Any,
) -> None:
    records.record_effect(
        receipt,
        consumer_id="observation_index",
        target_effect_id=(
            f"observation-calibration:{receipt.observation_id}:{receipt.revision_id}"
        ),
        before_hash=sha256_json(report.input_snapshot["observation"]),
        after_hash=sha256_json(
            {
                "observation_id": receipt.observation_id,
                "calibration_revision_id": receipt.revision_id,
                "calibration_record_hash": receipt.payload_hash,
                "posterior": report.calibrated_confidence,
            }
        ),
        evidence_refs=(
            f"observation:{receipt.observation_id}",
            f"calibration-revision:{receipt.revision_id}",
        ),
    )


def _close_retired_generation_commands(
    state_store: CognitiveStateStore,
    *,
    successor_by_revision: dict[str, str],
    retired_collisions: dict[str, str],
) -> int:
    """Truthfully close commands whose immutable generation is no longer current."""

    closed = 0
    for command in state_store.pending_commands():
        old_revision_id = str(command["revision_id"])
        successor = successor_by_revision.get(old_revision_id)
        collision_hash = retired_collisions.get(old_revision_id)
        if successor:
            target_effect_id = f"superseded-calibration:{old_revision_id}:{successor}"
            evidence_refs = (
                f"calibration-revision:{old_revision_id}",
                f"calibration-revision:{successor}",
            )
            reason = "superseded_by_current_calibration_replay"
        elif collision_hash:
            target_effect_id = f"retired-system-collision:{old_revision_id}"
            evidence_refs = (
                f"calibration-revision:{old_revision_id}",
                f"collision-contract:{collision_hash}",
            )
            reason = "retired_legacy_system_identity_collision"
        else:
            continue
        state_store.record_effect_receipt(
            str(command["command_id"]),
            status="intentional_skip",
            target_effect_id=target_effect_id,
            evidence_refs=evidence_refs,
            outcome="immutable calibration generation is no longer current",
            terminal_reason_code=reason,
        )
        closed += 1
    return closed


def _close_planned_historical_commands(
    state_store: CognitiveStateStore,
    plan: CalibrationReconciliationPlan,
) -> int:
    closed = 0
    for closure in plan.command_closures:
        command = state_store.command(closure.command_id)
        current = state_store.current_revision("calibration_record", closure.object_id)
        old = state_store.revision(closure.old_revision_id)
        if (
            command is None
            or str(command["revision_id"]) != closure.old_revision_id
            or state_store.effect_receipt(closure.command_id) is not None
            or old is None
            or old.payload_hash != closure.old_payload_hash
            or current is None
            or current.revision_id != closure.current_revision_id
            or current.payload_hash != closure.current_payload_hash
        ):
            raise RuntimeError("historical calibration command precondition drifted")
        state_store.record_effect_receipt(
            closure.command_id,
            status="intentional_skip",
            target_effect_id=(
                f"superseded-calibration:{closure.old_revision_id}:"
                f"{closure.current_revision_id}"
            ),
            evidence_refs=(
                f"calibration-revision:{closure.old_revision_id}",
                f"calibration-revision:{closure.current_revision_id}",
            ),
            outcome="immutable calibration generation is no longer current",
            terminal_reason_code="superseded_by_current_calibration_replay",
        )
        closed += 1
    return closed


def _record_migration(
    paths: CalibrationReconciliationPaths,
    *,
    plan: CalibrationReconciliationPlan,
    status: str,
    backup_ref: str,
    verification: dict[str, Any],
    error: str = "",
) -> str:
    suffix = sha256_json(
        {
            "inventory_hash": plan.inventory_hash,
            "status": status,
            "created_at": _now(),
        }
    ).split(":", 1)[1][:32]
    record = MigrationLedgerRecord(
        ledger_id=f"calibration-migration-{suffix}",
        migration_id=MIGRATION_ID,
        status=status,
        plan_hash=plan.inventory_hash,
        from_version="historical-calibration-spec",
        to_version=plan.validator_spec_hash,
        backup_ref=backup_ref,
        actor="local_operator",
        verification=verification,
        rollback_ref=backup_ref,
        error=error,
    )
    return MigrationLedger(paths.migrations_path).record(record)


def apply_calibration_reconciliation(
    paths: CalibrationReconciliationPaths,
    *,
    expected_inventory_hash: str,
    backup_dir: Path,
    engine: CalibrationEngine | None = None,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply one reviewed plan, rolling all participating stores back on failure."""

    current_engine = engine or CalibrationEngine()
    plan = build_calibration_reconciliation_plan(paths, engine=current_engine)
    result = plan.as_dict()
    if not plan.ok:
        return result
    if not expected_inventory_hash:
        return {**result, "ok": False, "status": "blocked", "error": "expected_hash_required"}
    if expected_inventory_hash != plan.inventory_hash:
        return {**result, "ok": False, "status": "blocked", "error": "inventory_hash_mismatch"}
    if not plan.requires_apply:
        return {**result, "status": "noop", "applied": False, "backups": []}
    if not runtime_writers_are_inactive(paths.database_dir):
        return {**result, "ok": False, "status": "blocked", "error": "daemon_not_inactive"}

    backup_root = Path(backup_dir).expanduser()
    sqlite_sources = [paths.state_path, paths.migrations_path]
    if plan.replays or plan.retirements:
        sqlite_sources.append(paths.observations_path)
    backups = backup_sqlite_databases(
        sqlite_sources,
        backup_root,
    )
    if plan.replays or plan.retirements:
        backups.extend(backup_projection_files(paths.projection_dir, backup_root))
    backup_ref = json.dumps(backups, ensure_ascii=False, sort_keys=True)
    _record_migration(
        paths,
        plan=plan,
        status="applying",
        backup_ref=backup_ref,
        verification={"reviewed_inventory_hash": expected_inventory_hash},
    )

    try:
        state_store = CognitiveStateStore(paths.state_path)
        records = CalibrationRecordStore(state_store)
        observation_store = (
            ObservationStore(str(paths.observations_path))
            if plan.replays or plan.retirements
            else None
        )
        applied_revisions: list[str] = []
        successor_by_revision: dict[str, str] = {}
        for index, replay in enumerate(plan.replays):
            current = state_store.current_revision("calibration_record", replay.object_id)
            if current is None or (
                current.revision_id != replay.old_revision_id
                or current.payload_hash != replay.old_payload_hash
            ):
                raise RuntimeError("calibration head drifted after reviewed plan")
            receipt, persisted = records.commit(replay.observation, replay.report)
            if (
                receipt.payload_hash != replay.expected_payload_hash
                or persisted.calculation_input_hash != replay.expected_input_hash
            ):
                raise RuntimeError("calibration replay result differs from reviewed plan")
            assert observation_store is not None
            records.apply_to_observation(observation_store, receipt)
            _record_observation_effect(records, receipt, persisted)
            applied_revisions.append(receipt.revision_id)
            successor_by_revision[replay.old_revision_id] = receipt.revision_id
            if failpoint is not None:
                failpoint(f"replay:{index}")

        for index, retirement in enumerate(plan.retirements):
            _retire_collision(paths, retirement)
            if failpoint is not None:
                failpoint(f"retirement:{index}")

        projection_dimensions = {
            replay.dimension for replay in plan.replays
        } | {"attention" for _ in plan.retirements}
        if projection_dimensions:
            assert observation_store is not None
            projection_engine = ObservationEngine(
                store=observation_store,
                wiki_dir=str(paths.wiki_dir),
                cognitive_state_store=state_store,
                calibration_engine=current_engine,
            )
            projection_engine._reexport_all(dimensions=projection_dimensions)
        closed_planned_commands = _close_planned_historical_commands(state_store, plan)
        closed_retired_commands = _close_retired_generation_commands(
            state_store,
            successor_by_revision=successor_by_revision,
            retired_collisions={
                value.old_revision_id: value.collision_contract_hash
                for value in plan.retirements
            },
        )
        if failpoint is not None:
            failpoint("projection")

        post_plan = build_calibration_reconciliation_plan(paths, engine=current_engine)
        if not post_plan.ok or post_plan.requires_apply:
            raise RuntimeError("post-apply calibration inventory is not clean")
        expected_current = plan.current_count - len(plan.retirements)
        if post_plan.current_count != expected_current:
            raise RuntimeError("post-apply calibration head count mismatch")
        integrity = _verify_integrity(paths)
        ledger_id = _record_migration(
            paths,
            plan=plan,
            status="verified",
            backup_ref=backup_ref,
            verification={
                "reviewed_inventory_hash": expected_inventory_hash,
                "post_inventory_hash": post_plan.inventory_hash,
                "applied_revision_count": len(applied_revisions),
                "retired_collision_count": len(plan.retirements),
                "closed_retired_generation_commands": closed_retired_commands,
                "closed_planned_historical_commands": closed_planned_commands,
                "sqlite_integrity": integrity,
            },
        )
        return {
            **post_plan.as_dict(),
            "status": "verified",
            "applied": True,
            "reviewed_inventory_hash": expected_inventory_hash,
            "applied_revision_count": len(applied_revisions),
            "retired_collision_count": len(plan.retirements),
            "closed_retired_generation_commands": closed_retired_commands,
            "closed_planned_historical_commands": closed_planned_commands,
            "ledger_id": ledger_id,
            "backups": backups,
            "sqlite_integrity": integrity,
        }
    except BaseException as exc:
        restore_backups(reversed(backups))
        rolled_back = build_calibration_reconciliation_plan(
            paths,
            engine=current_engine,
        )
        rollback_ok = rolled_back.inventory_hash == plan.inventory_hash
        _record_migration(
            paths,
            plan=plan,
            status="failed",
            backup_ref=backup_ref,
            verification={
                "reviewed_inventory_hash": expected_inventory_hash,
                "rollback_inventory_hash": rolled_back.inventory_hash,
                "rollback_verified": rollback_ok,
            },
            error=exc.__class__.__name__,
        )
        return {
            **rolled_back.as_dict(),
            "ok": False,
            "status": "rolled_back" if rollback_ok else "failed",
            "error": str(exc),
            "rollback_verified": rollback_ok,
            "backups": backups,
        }


__all__ = ["apply_calibration_reconciliation"]
