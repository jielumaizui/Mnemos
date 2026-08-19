"""Backup-first executor for reviewed cognitive-action state reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable

from core.cognitive.calibration_reconcile_backup import (
    backup_sqlite_databases,
    restore_backups,
)
from core.hephaestus.cognitive_action_state_reconcile_contracts import (
    CognitiveActionStateReconciliationPaths,
    CognitiveActionStateReconciliationPlan,
    MIGRATION_CONTRACT_HASH,
    RECONCILIATION_BATCH_TABLE,
    RECONCILIATION_SCHEMA_SQL,
    RECONCILIATION_SCHEMA_VERSION,
    RECONCILIATION_TABLE,
    make_batch_id,
    reconciliation_schema_is_valid,
)
from core.hephaestus.cognitive_action_state_reconcile_planner import (
    build_cognitive_action_state_reconciliation_plan,
)
from core.hephaestus.cognitive_action_targets import (
    TARGET_STATE_HASH_CONTRACT_VERSION,
)
from core.hephaestus.distill_action_store import canonical_json
from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_integrity(
    paths: CognitiveActionStateReconciliationPaths,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in (paths.action_path, paths.observations_path):
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            result[path.name] = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
    if any(value != "ok" for value in result.values()):
        raise RuntimeError("post-apply SQLite integrity check failed")
    return result


def _apply_rows(
    paths: CognitiveActionStateReconciliationPaths,
    *,
    plan: CognitiveActionStateReconciliationPlan,
    failpoint: Callable[[str], None] | None,
) -> int:
    applied_at = _now()
    batch_id = make_batch_id(plan.inventory_hash, plan.object_manifest_hash)
    manifests = [candidate.manifest() for candidate in plan.candidates]
    with sqlite3.connect(paths.observations_path, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("BEGIN IMMEDIATE;\n" + RECONCILIATION_SCHEMA_SQL)
        if not reconciliation_schema_is_valid(connection):
            raise RuntimeError("target-state reconciliation schema validation failed")
        connection.execute(
            f"""
            INSERT INTO {RECONCILIATION_BATCH_TABLE} (
                batch_id, schema_version, migration_contract_hash,
                state_contract_version, inventory_hash, object_manifest_hash,
                object_count, inventory_manifest_json, object_manifest_json,
                applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,  # nosec B608
            (
                batch_id,
                RECONCILIATION_SCHEMA_VERSION,
                MIGRATION_CONTRACT_HASH,
                TARGET_STATE_HASH_CONTRACT_VERSION,
                plan.inventory_hash,
                plan.object_manifest_hash,
                len(manifests),
                plan.inventory_manifest_json,
                canonical_json(manifests),
                applied_at,
            ),
        )
        for candidate in plan.candidates:
            connection.execute(
                f"""
                INSERT INTO {RECONCILIATION_TABLE} (
                    reconciliation_id, batch_id, effect_id,
                    cognitive_action_id, action, target, target_object_id,
                    recorded_contract_version, recorded_after_hash, artifact_hash,
                    command_hash, effect_hash,
                    original_receipt_hash, target_row_hash,
                    expected_state_hash, current_state_hash,
                    state_contract_version, migration_contract_hash,
                    inventory_hash, object_manifest_hash, applied_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?
                )
                """,  # nosec B608
                (
                    candidate.reconciliation_id,
                    batch_id,
                    candidate.effect_id,
                    candidate.cognitive_action_id,
                    candidate.action,
                    candidate.target,
                    candidate.target_object_id,
                    candidate.recorded_contract_version,
                    candidate.recorded_after_hash,
                    candidate.artifact_hash,
                    candidate.command_hash,
                    candidate.effect_hash,
                    candidate.original_receipt_hash,
                    candidate.target_row_hash,
                    candidate.expected_state_hash,
                    candidate.current_state_hash,
                    TARGET_STATE_HASH_CONTRACT_VERSION,
                    MIGRATION_CONTRACT_HASH,
                    plan.inventory_hash,
                    plan.object_manifest_hash,
                    applied_at,
                ),
            )
        if failpoint is not None:
            failpoint("after_reconciliation_insert")
        connection.commit()
    return len(manifests)


def apply_cognitive_action_state_reconciliation(
    paths: CognitiveActionStateReconciliationPaths,
    *,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
    backup_dir: Path,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply one exact reviewed plan and restore both stores on any failure."""

    plan = build_cognitive_action_state_reconciliation_plan(paths)
    report = plan.as_dict()
    if not plan.ok:
        return report
    if not expected_inventory_hash or not expected_object_manifest_hash:
        return {
            **report,
            "ok": False,
            "status": "blocked",
            "error": "reviewed_hashes_required",
        }
    if expected_inventory_hash != plan.inventory_hash:
        return {
            **report,
            "ok": False,
            "status": "blocked",
            "error": "inventory_hash_mismatch",
        }
    if expected_object_manifest_hash != plan.object_manifest_hash:
        return {
            **report,
            "ok": False,
            "status": "blocked",
            "error": "object_manifest_hash_mismatch",
        }
    if not plan.requires_apply:
        return {**report, "status": "noop", "applied": False, "backups": []}
    if not runtime_writers_are_inactive(paths.database_dir):
        return {
            **report,
            "ok": False,
            "status": "blocked",
            "error": "mnemos_runtime_active",
        }

    backups = backup_sqlite_databases(
        (paths.action_path, paths.observations_path),
        Path(backup_dir),
        label="cognitive-action-state-v3",
    )
    if not runtime_writers_are_inactive(paths.database_dir):
        return {
            **report,
            "ok": False,
            "status": "blocked",
            "error": "mnemos_runtime_became_active",
            "backups": backups,
        }
    fresh = build_cognitive_action_state_reconciliation_plan(paths)
    if (
        fresh.inventory_hash != plan.inventory_hash
        or fresh.object_manifest_hash != plan.object_manifest_hash
    ):
        return {
            **fresh.as_dict(),
            "ok": False,
            "status": "blocked",
            "error": "inventory_drift_after_backup",
            "backups": backups,
        }

    try:
        applied_count = _apply_rows(paths, plan=plan, failpoint=failpoint)
        post_plan = build_cognitive_action_state_reconciliation_plan(paths)
        if not post_plan.ok or post_plan.requires_apply:
            raise RuntimeError("post-apply target-state inventory is not clean")
        integrity = _verify_integrity(paths)
        return {
            **post_plan.as_dict(),
            "status": "verified",
            "applied": True,
            "reviewed_inventory_hash": plan.inventory_hash,
            "reviewed_object_manifest_hash": plan.object_manifest_hash,
            "applied_count": applied_count,
            "backups": backups,
            "sqlite_integrity": integrity,
        }
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        restore_backups(reversed(backups))
        restored = build_cognitive_action_state_reconciliation_plan(paths)
        rollback_verified = bool(
            restored.inventory_hash == plan.inventory_hash
            and restored.object_manifest_hash == plan.object_manifest_hash
        )
        return {
            **restored.as_dict(),
            "ok": False,
            "status": "rolled_back" if rollback_verified else "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "rollback_verified": rollback_verified,
            "backups": backups,
        }


__all__ = ["apply_cognitive_action_state_reconciliation"]
