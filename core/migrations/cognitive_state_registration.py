"""Migration-registry descriptors for dedicated cognitive databases."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Callable

COGNITIVE_STATE_STORE_MIGRATION_ID = "database.cognitive_state_store.v1"
ACTION_LEDGER_MIGRATION_ID = "database.action_ledger_append_only.v1"
DECISION_TRACE_HISTORY_MIGRATION_ID = "database.decision_trace_history.v1"
MATERIAL_EFFECT_SCHEMA_MIGRATION_ID = "database.material_effect_schema.v1"
DEDICATED_DATABASE_MIGRATIONS = frozenset(
    {
        COGNITIVE_STATE_STORE_MIGRATION_ID,
        ACTION_LEDGER_MIGRATION_ID,
        DECISION_TRACE_HISTORY_MIGRATION_ID,
        MATERIAL_EFFECT_SCHEMA_MIGRATION_ID,
    }
)


def dedicated_migration_spec_kwargs() -> tuple[dict[str, Any], ...]:
    return (
        {
            "migration_id": COGNITIVE_STATE_STORE_MIGRATION_ID,
            "from_version": "runtime-producer-consumer-v2",
            "to_version": "cognitive-state-store-v1",
            "scope": "database",
            "risk_level": "high",
            "summary": (
                "Atomically reconcile typed cognitive revisions, immutable event envelopes, "
                "consumer-pair heads, and the local outbox."
            ),
            "affected_paths": (
                "core/cognitive/state_schema.py",
                "core/cognitive/state_store.py",
                "database:producer_consumer_ledger.db",
            ),
            "requires_backup": True,
            "wrapper_command": (
                "python3",
                "scripts/reconcile_cognitive_state_store.py",
                "--json",
            ),
            "capability_refs": (
                "cognitive_state_store",
                "producer_consumer_ledger",
                "migration_registry",
            ),
        },
        {
            "migration_id": ACTION_LEDGER_MIGRATION_ID,
            "from_version": "mutable-action-ledger-v0",
            "to_version": "append-only-action-ledger-v1",
            "scope": "database",
            "risk_level": "high",
            "summary": "Make action evidence immutable and reject update/delete/replace semantics.",
            "affected_paths": (
                "core/ops/action_ledger_schema.py",
                "core/ops/action_ledger.py",
                "database:action_ledger.db",
            ),
            "requires_backup": True,
            "wrapper_command": (
                "python3",
                "scripts/reconcile_action_ledger.py",
                "--json",
            ),
            "capability_refs": ("action_ledger", "migration_registry"),
        },
        {
            "migration_id": DECISION_TRACE_HISTORY_MIGRATION_ID,
            "from_version": "material-actions-without-object-provenance-v0",
            "to_version": "decision-trace-history-v1",
            "scope": "database",
            "risk_level": "critical",
            "summary": (
                "Inventory each legacy material-action object, quarantine incomplete "
                "history without inventing cognition, and activate strict enforcement."
            ),
            "affected_paths": (
                "core/cognitive/decision_trace_migration.py",
                "scripts/reconcile_decision_trace_history.py",
                "database:action_ledger.db",
                "database:delivery_events.db",
                "database:trusted_push.db",
                "database:producer_consumer_ledger.db",
            ),
            "requires_backup": True,
            "wrapper_command": (
                "python3",
                "scripts/reconcile_decision_trace_history.py",
                "--json",
            ),
            "capability_refs": (
                "decision_trace",
                "cognitive_state_store",
                "migration_registry",
            ),
        },
        {
            "migration_id": MATERIAL_EFFECT_SCHEMA_MIGRATION_ID,
            "from_version": "runtime-created-material-effect-journal-v0",
            "to_version": "registered-material-effect-schema-v1",
            "scope": "database",
            "risk_level": "high",
            "summary": (
                "Reconcile target-local effect journals under one registered schema "
                "authority before any material-action writer opens."
            ),
            "affected_paths": (
                "core/cognitive/material_effect_schema.py",
                "scripts/reconcile_material_effect_schema.py",
                "database:policy_patches.db",
                "database:user_signals.db",
                "database:cognitive_graph.db",
                "database:knowledge_graph.db",
            ),
            "requires_backup": True,
            "wrapper_command": (
                "python3",
                "scripts/reconcile_material_effect_schema.py",
                "--json",
            ),
            "capability_refs": (
                "material_action_effects",
                "decision_trace",
                "migration_registry",
            ),
        },
    )


def inspect_dedicated_migration(
    migration_id: str,
    config: Any,
) -> tuple[str, str]:
    database_dir = Path(config.database_dir).expanduser()
    inspector: Callable[[sqlite3.Connection], Any]
    if migration_id == COGNITIVE_STATE_STORE_MIGRATION_ID:
        from core.cognitive.state_schema import inspect_cognitive_state_schema

        db_path = database_dir / "producer_consumer_ledger.db"
        inspector = inspect_cognitive_state_schema
    elif migration_id == ACTION_LEDGER_MIGRATION_ID:
        from core.ops.action_ledger_schema import inspect_action_ledger_schema

        db_path = database_dir / "action_ledger.db"
        inspector = inspect_action_ledger_schema
    elif migration_id == DECISION_TRACE_HISTORY_MIGRATION_ID:
        from core.cognitive.decision_trace_migration import (
            configured_source_domains,
            inspect_decision_trace_history_coverage,
        )

        domains = configured_source_domains(config=config, database_dir=database_dir)
        coverage = inspect_decision_trace_history_coverage(
            domains,
            database_dir / "producer_consumer_ledger.db"
        )
        if coverage["ok"] or not coverage["initialized_source_count"]:
            return "verified", "decision-trace enforcement target is verified"
        return (
            "planned",
            "run object inventory, reviewed backup, and decision-history reconciliation",
        )
    elif migration_id == MATERIAL_EFFECT_SCHEMA_MIGRATION_ID:
        from core.cognitive.material_effect_schema import (
            configured_material_effect_databases,
            inspect_material_effect_schema,
        )

        initialized = 0
        for db_path in configured_material_effect_databases(config):
            if not db_path.is_file():
                continue
            initialized += 1
            with sqlite3.connect(
                f"file:{db_path.resolve(strict=True)}?mode=ro",
                uri=True,
            ) as conn:
                if not inspect_material_effect_schema(conn).ok:
                    return (
                        "planned",
                        "run exact-hash material-effect schema reconciliation",
                    )
        if initialized:
            return "verified", "all initialized material-effect schemas are canonical"
        return (
            "verified",
            "target databases are not initialized; fresh provisioning is canonical",
        )
    else:
        raise KeyError(f"not a dedicated cognitive database migration: {migration_id}")
    if not db_path.is_file():
        return (
            "verified",
            "database is not initialized; fresh provisioning uses canonical schema",
        )
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        state = inspector(conn)
    if state.ok:
        return "verified", "canonical schema already verified"
    return "planned", "run dedicated dry-run, backup, and apply reconciliation command"
