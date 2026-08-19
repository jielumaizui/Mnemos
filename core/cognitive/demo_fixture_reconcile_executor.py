"""Apply an exact reviewed demo-fixture leak reconciliation plan."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from core.cognitive.calibration_reconcile_backup import (
    backup_sqlite_databases,
    restore_backups,
)
from core.cognitive.demo_fixture_reconcile_contracts import (
    DemoFixtureEpisodeRetirement,
    DemoFixtureReconciliationPaths,
    DemoFixtureReconciliationPlan,
    MIGRATION_ID,
)
from core.cognitive.demo_fixture_reconcile_planner import (
    build_demo_fixture_reconciliation_plan,
)
from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive
from core.migrations.registry import MigrationLedger, MigrationLedgerRecord
from core.ops.producer_consumer_ledger import ProducerConsumerLedger


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_hash(row: sqlite3.Row) -> str:
    return sha256_json({str(key): row[key] for key in row.keys()})


def _record_migration(
    paths: DemoFixtureReconciliationPaths,
    *,
    plan: DemoFixtureReconciliationPlan,
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
    return MigrationLedger(paths.migrations_path).record(
        MigrationLedgerRecord(
            ledger_id=f"demo-fixture-migration-{suffix}",
            migration_id=MIGRATION_ID,
            status=status,
            plan_hash=plan.inventory_hash,
            from_version="fixture-telemetry-in-production",
            to_version="isolated-demo-fixture-v1",
            backup_ref=backup_ref,
            actor="local_operator",
            verification=verification,
            rollback_ref=backup_ref,
            error=error,
        )
    )


def _retire_episode(
    paths: DemoFixtureReconciliationPaths,
    plan: DemoFixtureReconciliationPlan,
    episode: DemoFixtureEpisodeRetirement,
) -> str:
    quarantine_id = (
        "demo-fixture-quarantine-" + sha256_json(episode.manifest()).split(":", 1)[1][:32]
    )
    with sqlite3.connect(paths.state_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        revision = conn.execute(
            """
            SELECT r.*
            FROM cognitive_state_heads AS h
            JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
            WHERE h.object_type='cognition_episode' AND h.object_id=?
            """,
            (episode.object_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM cognitive_data_events WHERE event_id=?",
            (episode.event_id,),
        ).fetchone()
        if (
            revision is None
            or event is None
            or str(revision["revision_id"]) != episode.revision_id
            or str(revision["payload_hash"]) != episode.payload_hash
            or _row_hash(revision) != episode.revision_row_hash
            or _row_hash(event) != episode.event_row_hash
        ):
            raise RuntimeError("demo cognition retirement precondition drifted")
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
                episode.revision_id,
                "synthetic_fixture_source_not_in_canonical_raw",
                canonical_json(
                    {
                        "object_id": episode.object_id,
                        "event_id": episode.event_id,
                        "source_revision_ids": list(episode.source_revision_ids),
                        "command_ids": [value.command_id for value in episode.commands],
                    }
                ),
                canonical_json(
                    {
                        "schema_version": "mnemos.demo_fixture_quarantine.v1",
                        "object_type": "cognition_episode",
                        "object_id": episode.object_id,
                        "revision_id": episode.revision_id,
                        "payload_hash": episode.payload_hash,
                        "fixture_source_hash": plan.fixture_source_hash,
                    }
                ),
                episode.payload_hash,
                _now(),
            ),
        )
        conn.execute(
            "DELETE FROM cognitive_state_heads "
            "WHERE object_type='cognition_episode' AND object_id=?",
            (episode.object_id,),
        )
        conn.commit()
    return quarantine_id


def _close_episode_commands(
    paths: DemoFixtureReconciliationPaths,
    plan: DemoFixtureReconciliationPlan,
    episode: DemoFixtureEpisodeRetirement,
    quarantine_id: str,
) -> int:
    state = CognitiveStateStore(paths.state_path)
    closed = 0
    for expected in episode.commands:
        command = state.command(expected.command_id)
        if (
            command is None
            or str(command["revision_id"]) != episode.revision_id
            or str(command["consumer_id"]) != expected.consumer_id
            or str(command["payload_hash"]) != expected.payload_hash
            or state.effect_receipt(expected.command_id) is not None
        ):
            raise RuntimeError("demo cognition command precondition drifted")
        state.record_cognition_episode_omission_receipt(
            expected.command_id,
            quarantine_id=quarantine_id,
        )
        closed += 1
    return closed


def _skip_quality_actions(
    paths: DemoFixtureReconciliationPaths,
    plan: DemoFixtureReconciliationPlan,
) -> int:
    ledger = ProducerConsumerLedger(paths.database_dir, initialize=False)
    skipped = 0
    with (
        sqlite3.connect(paths.action_path) as action_conn,
        sqlite3.connect(paths.state_path) as state_conn,
    ):
        action_conn.row_factory = sqlite3.Row
        state_conn.row_factory = sqlite3.Row
        for action in plan.actions:
            action_row = action_conn.execute(
                "SELECT * FROM action_ledger WHERE action_id=?",
                (action.action_id,),
            ).fetchone()
            event_row = state_conn.execute(
                "SELECT * FROM runtime_flow_events WHERE event_id=?",
                (action.production_event_id,),
            ).fetchone()
            if (
                action_row is None
                or event_row is None
                or _row_hash(action_row) != action.action_row_hash
                or _row_hash(event_row) != action.runtime_event_row_hash
            ):
                raise RuntimeError("demo quality action precondition drifted")
            ledger.record_skipped(
                "distill_quality_to_write_admission",
                source=action.consumer_id,
                item_id=action.action_id,
                production_event_id=action.production_event_id,
                metadata={
                    "transition": "write_admission_terminal",
                    "terminal_reason": "synthetic_fixture_not_written_to_production",
                    "action_row_hash": action.action_row_hash,
                    "runtime_event_row_hash": action.runtime_event_row_hash,
                    "fixture_source_hash": plan.fixture_source_hash,
                },
                idempotency_key=(
                    "demo-fixture-skip:" f"{action.production_event_id}:{action.consumer_id}"
                ),
            )
            skipped += 1
    return skipped


def _verify_integrity(paths: DemoFixtureReconciliationPaths) -> dict[str, str]:
    results: dict[str, str] = {}
    for path in (
        paths.state_path,
        paths.raw_path,
        paths.action_path,
        paths.migrations_path,
    ):
        if not path.is_file():
            continue
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            results[path.name] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if any(value != "ok" for value in results.values()):
        raise RuntimeError("post-apply SQLite integrity check failed")
    return results


def apply_demo_fixture_reconciliation(
    paths: DemoFixtureReconciliationPaths,
    *,
    expected_inventory_hash: str,
    backup_dir: Path,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply one reviewed object manifest and roll back every mutation on failure."""

    plan = build_demo_fixture_reconciliation_plan(paths)
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

    backups = backup_sqlite_databases(
        (paths.state_path, paths.action_path, paths.migrations_path),
        Path(backup_dir).expanduser(),
        label="demo-fixture-reconcile",
    )
    backup_ref = json.dumps(backups, ensure_ascii=False, sort_keys=True)
    _record_migration(
        paths,
        plan=plan,
        status="applying",
        backup_ref=backup_ref,
        verification={"reviewed_inventory_hash": expected_inventory_hash},
    )
    try:
        retired = 0
        closed_commands = 0
        for index, episode in enumerate(plan.episodes):
            quarantine_id = _retire_episode(paths, plan, episode)
            closed_commands += _close_episode_commands(
                paths,
                plan,
                episode,
                quarantine_id,
            )
            retired += 1
            if failpoint is not None:
                failpoint(f"episode:{index}")
        skipped_actions = _skip_quality_actions(paths, plan)
        if failpoint is not None:
            failpoint("actions")

        post_plan = build_demo_fixture_reconciliation_plan(paths)
        if not post_plan.ok or post_plan.requires_apply:
            raise RuntimeError("post-apply demo fixture inventory is not clean")
        integrity = _verify_integrity(paths)
        ledger_id = _record_migration(
            paths,
            plan=plan,
            status="verified",
            backup_ref=backup_ref,
            verification={
                "reviewed_inventory_hash": expected_inventory_hash,
                "post_inventory_hash": post_plan.inventory_hash,
                "retired_episode_count": retired,
                "closed_command_count": closed_commands,
                "skipped_action_count": skipped_actions,
                "sqlite_integrity": integrity,
            },
        )
        return {
            **post_plan.as_dict(),
            "status": "verified",
            "applied": True,
            "reviewed_inventory_hash": expected_inventory_hash,
            "retired_episode_count": retired,
            "closed_command_count": closed_commands,
            "skipped_action_count": skipped_actions,
            "ledger_id": ledger_id,
            "backups": backups,
            "sqlite_integrity": integrity,
        }
    except BaseException as exc:
        restore_backups(reversed(backups))
        rolled_back = build_demo_fixture_reconciliation_plan(paths)
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


__all__ = ["apply_demo_fixture_reconciliation"]
