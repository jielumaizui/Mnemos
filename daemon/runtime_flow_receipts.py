"""Daemon-owned runtime flow bootstrap and raw-projection episode receipts."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from daemon import service_state


def bootstrap_runtime_flow_ledger(
    cfg: Any,
    *,
    log: logging.Logger,
) -> None:
    database_dir = getattr(cfg, "database_dir", None)
    if not isinstance(database_dir, (str, Path)):
        log.debug("[DAEMON] runtime flow ledger bootstrap skipped: invalid database_dir")
        return
    from core.ops.runtime_flow_health import (
        bootstrap_runtime_producer_consumer_ledger,
    )

    result = bootstrap_runtime_producer_consumer_ledger(cfg)
    log.info(
        "[DAEMON] runtime flow ledger ready: %d flows, %d migrated, %d replayed",
        result["registered_flows"],
        result["migrated_legacy_receipts"],
        result["replayed_outbox_events"],
    )


def record_service_recovery_action(
    service_name: str,
    previous_error: dict[str, Any],
    result: dict[str, Any],
    cfg: Any,
    *,
    log: logging.Logger,
) -> None:
    if service_name != "raw_projection":
        return
    try:
        from core.system_contracts import ActionLedger, make_action_record
        from core.cognitive.state_contract import sha256_json
        from core.ops.action_ledger import authorize_primary_action_ledger_record

        record = make_action_record(
            actor="mnemos_daemon",
            action_type="raw_projection_recovered",
            target=service_name,
            evidence_refs=("mnemos_daemon.py", "daemon/heartbeat.py"),
            status="verified",
            verification={
                "service": service_name,
                "status": result.get("status"),
                "last_error": previous_error.get("last_error"),
                "last_error_type": previous_error.get("last_error_type"),
                "last_error_context": previous_error.get("last_context"),
                "previous_error_count": previous_error.get("count", 0),
            },
        )
        material_action = authorize_primary_action_ledger_record(
            record,
            state_db_path=Path(cfg.database_dir) / "producer_consumer_ledger.db",
            contract_id="project-contract:raw-projection-recovery-ledger",
            contract_revision_id="mnemos.raw_projection_recovery_ledger.v1",
            contract_text=(
                "Append a raw-projection recovery result only after a known "
                "raw-projection error was cleared and the recovery result is bound."
            ),
            source_namespace="raw-projection-recovery-ledger",
            source_facts={
                "service": service_name,
                "result_status": str(result.get("status") or ""),
                "last_error_type": str(previous_error.get("last_error_type") or ""),
                "previous_error_count": int(previous_error.get("count", 0) or 0),
            },
            decision_checks={
                "service_is_raw_projection": service_name == "raw_projection",
                "prior_error_exists": int(previous_error.get("count", 0) or 0) > 0,
                "recovery_result_exists": bool(result.get("status")),
            },
            evidence_refs=("mnemos_daemon.py", "daemon/heartbeat.py"),
            task="Append the exact raw-projection recovery result",
            goal="Preserve a bound recovery receipt without relabeling the action.",
            constraints=(
                "Do not record recovery without a prior raw-projection error.",
                "The authorization governs only this ActionLedger append.",
            ),
            producer="daemon-runtime-flow-receipts",
            producer_version="mnemos.raw_projection_recovery_ledger.v1",
            producer_code_hash=sha256_json(
                {
                    "module": "daemon.runtime_flow_receipts",
                    "contract": "mnemos.raw_projection_recovery_ledger.v1",
                }
            ),
            evaluator_id="raw-projection-recovery-ledger-evaluator",
            approved_candidate_key="append_bound_raw_projection_recovery",
            approved_candidate_summary="Append the exact cleared recovery result.",
            rejected_candidate_key="omit_unbound_raw_projection_recovery",
            rejected_candidate_summary="Do not append an unbound recovery claim.",
            approved_reason_code="raw_projection_recovery_binding_verified",
            rejected_reason_code="raw_projection_recovery_binding_rejected",
            committed_metric="raw_projection_recovery_action_ledger_receipt",
            rejected_metric="unbound_raw_projection_recovery_count",
        )
        ledger_id = ActionLedger.from_config(cfg, initialize=True).record(
            record,
            material_action=material_action,
        )
        from core.ops.runtime_flow_telemetry import (
            record_runtime_consumed,
            runtime_item_id,
        )

        record_runtime_consumed(
            "raw_projection_error_to_recovery_ledger",
            source="mnemos_daemon.py:recovery_action",
            item_id=runtime_item_id(
                "raw-projection-error",
                previous_error.get("first_error_at") or previous_error.get("last_error_at") or "",
            ),
            metadata={
                "transition": "recovery_action_recorded",
                "action_ledger_id": ledger_id,
            },
            config_or_path=cfg,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
        sqlite3.Error,
    ):
        log.debug("记录服务恢复 ActionLedger 失败: %s", service_name, exc_info=True)


def record_raw_projection_error(
    cfg: Any,
    error_state: dict[str, dict[str, Any]],
    exc: Exception,
) -> None:
    state = error_state.get(service_state.service_error_key("raw_projection"), {})
    from core.ops.runtime_flow_telemetry import record_runtime_produced, runtime_item_id

    record_runtime_produced(
        "raw_projection_error_to_recovery_ledger",
        source="mnemos_daemon.py:raw_projection",
        item_id=runtime_item_id(
            "raw-projection-error",
            state.get("first_error_at") or state.get("last_error_at") or "",
        ),
        intended_consumers=["mnemos_daemon.py:recovery_action"],
        metadata={
            "transition": "raw_projection_error_recorded",
            "error_type": type(exc).__name__,
        },
        config_or_path=cfg,
    )
