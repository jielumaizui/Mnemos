"""Provisioning and read-only health reduction for runtime flow evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.cognitive.state_schema import CognitiveStateSchemaError
from core.ops.producer_consumer_ledger import (
    DEFAULT_MATRIX,
    SCHEMA_VERSION,
    ProducerConsumerLedger,
)


def bootstrap_runtime_producer_consumer_ledger(
    config: Any,
    *,
    matrix_path: Path = DEFAULT_MATRIX,
) -> dict[str, Any]:
    """Explicitly provision/migrate the ledger; health and audits never call this."""
    ledger = ProducerConsumerLedger(config, initialize=True)
    registered = ledger.register_adaptive_flows(Path(matrix_path))
    migrated = ledger.migrate_v1_terminal_events()
    from core.ops.runtime_flow_telemetry import RuntimeFlowTelemetry

    replayed = RuntimeFlowTelemetry(config).drain_outbox()
    return {
        "schema_version": SCHEMA_VERSION,
        "registered_flows": registered,
        "migrated_legacy_receipts": migrated,
        "replayed_outbox_events": replayed,
        "db_path": str(ledger.db_path),
    }


def build_runtime_producer_consumer_health(
    config: Any,
    *,
    matrix_path: Path | None = DEFAULT_MATRIX,
) -> dict[str, Any]:
    """Read runtime closure without creating or mutating its evidence store."""
    try:
        ledger = ProducerConsumerLedger(config, initialize=False, read_only=True)
        snapshot = ledger.snapshot()
    except (
        CognitiveStateSchemaError,
        FileNotFoundError,
        sqlite3.DatabaseError,
        json.JSONDecodeError,
        KeyError,
    ) as exc:
        if isinstance(exc, CognitiveStateSchemaError):
            error = f"runtime producer/consumer ledger requires reconciliation: {exc}"
        else:
            error = "runtime producer/consumer ledger is unavailable"
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "observation_state": "blocked",
            "counts": {"registered_flows": 0},
            "degraded_flows": [],
            "flows": {},
            "cognitive_data": {"status": "blocked", "counts": {}},
            "error": error,
            "error_type": type(exc).__name__,
        }
    if snapshot["status"] != "ok":
        snapshot["error"] = "runtime producer/consumer evidence is incomplete or stale"
    return snapshot


def audit_runtime_producer_consumer_closure(
    config: Any,
    *,
    strict: bool = False,
    matrix_path: Path | None = DEFAULT_MATRIX,
) -> list[str]:
    """Return strict closure errors for runtime producer/consumer ledgers."""
    health = build_runtime_producer_consumer_health(config, matrix_path=matrix_path)
    errors: list[str] = []
    if health.get("status") == "blocked":
        errors.append(str(health.get("error") or "runtime producer/consumer ledger blocked"))
    for flow_id, flow in health["flows"].items():
        produced = int(flow["produced_count"])
        consumed = int(flow["consumed_count"])
        dead_letters = int(flow["dead_letter_count"])
        pending = int(flow["pending_count"])
        overdue_pending = int(flow.get("overdue_pending_count", pending))
        orphan_items = int(flow.get("orphan_item_count", 0))
        no_source_items = int(flow.get("no_source_item_count", 0))
        observation_state = str(flow.get("observation_state") or "unknown")
        if observation_state == "unobserved":
            errors.append(f"{flow_id}: required flow has no runtime observations")
        elif observation_state == "stale":
            errors.append(f"{flow_id}: runtime observations are stale")
        missing_consumers = list(flow.get("missing_consumers") or [])
        extra_consumers = list(flow.get("extra_consumers") or [])
        if missing_consumers:
            errors.append(f"{flow_id}: missing terminal consumers {missing_consumers}")
        if extra_consumers:
            errors.append(f"{flow_id}: unexpected terminal consumers {extra_consumers}")
        if consumed > 0 and produced == 0:
            errors.append(f"{flow_id}: consumed {consumed} but no producer event was recorded")
        if produced > 0 and consumed == 0 and (
            overdue_pending > 0 or int(flow.get("intended_count", 0)) == 0
        ):
            errors.append(f"{flow_id}: produced {produced} but no consumer event was recorded")
        if produced > 0 and consumed > 0 and orphan_items > 0:
            errors.append(f"{flow_id}: {orphan_items} produced item ids were not consumed")
        if produced > 0 and consumed > 0 and no_source_items > 0:
            errors.append(f"{flow_id}: {no_source_items} consumed item ids had no producer")
        if overdue_pending > int(flow["pending_budget"]):
            errors.append(
                f"{flow_id}: overdue pending {overdue_pending} exceeds budget "
                f"{flow['pending_budget']} (total pending {pending})"
            )
        if dead_letters > int(flow["dead_letter_budget"]):
            errors.append(
                f"{flow_id}: produced {produced} but consumed {consumed} "
                f"with {dead_letters} dead letters"
            )
        if int(flow["lag_seconds"]) > int(flow["max_lag_seconds"]):
            errors.append(
                f"{flow_id}: lag {flow['lag_seconds']}s exceeds budget "
                f"{flow['max_lag_seconds']}s"
            )
    cognitive_data = health.get("cognitive_data", {})
    data_counts = cognitive_data.get("counts", {})
    if int(data_counts.get("unregistered_producers", 0)) > 0:
        errors.append(
            "cognitive_data: unregistered producers "
            f"{cognitive_data.get('unregistered_producers', [])}"
        )
    if int(data_counts.get("unregistered_consumers", 0)) > 0:
        errors.append(
            "cognitive_data: unregistered consumers "
            f"{cognitive_data.get('unregistered_consumers', [])}"
        )
    if int(data_counts.get("consumed_without_data_event", 0)) > 0:
        errors.append(
            "cognitive_data: consumed event ids missing data events "
            f"{cognitive_data.get('consumed_without_data_event_ids', [])}"
        )
    if int(data_counts.get("missing_intended_consumptions", 0)) > 0:
        errors.append(
            "cognitive_data: intended consumers missing terminal receipts "
            f"{cognitive_data.get('missing_intended_consumers', [])}"
        )
    if int(data_counts.get("extra_consumptions", 0)) > 0:
        errors.append(
            "cognitive_data: terminal receipts were not intended "
            f"{cognitive_data.get('extra_consumers', [])}"
        )
    if int(data_counts.get("duplicate_without_reconciliation", 0)) > 0:
        errors.append("cognitive_data: duplicate/derived/reinforcement relations missing")
    if int(data_counts.get("unexplained_divergence", 0)) > 0:
        errors.append("cognitive_data: unexplained divergence between sibling events")
    return errors if strict else []
