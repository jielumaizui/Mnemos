"""Read models and health inspection for the local model-call ledger."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from core.runtime_paths import RuntimePaths

from .contracts import (
    SCHEMA_VERSION,
    _FORBIDDEN_ENTRY_COLUMNS,
    ModelCallLedgerInvariantError,
)
from .normalization import (
    _canonical_run_id,
    _config_get,
    _is_canonical_run_id,
    _money_exceeds,
    _nonnegative_finite_float,
    _utc_day,
    _readonly_sqlite_connection,
)
from .lifecycle import LedgerLifecycle
from .schema_validation import LedgerSchemaValidation
from .state import LedgerState
from .subjects_retention import LedgerSubjectsRetention


class LedgerReporting:
    """Internal read model; it never provisions a ledger or changes SQLite state."""

    def __init__(
        self,
        state: LedgerState,
        *,
        lifecycle: LedgerLifecycle,
        retention: LedgerSubjectsRetention,
        validation: LedgerSchemaValidation,
    ):
        self._state = state
        self._lifecycle = lifecycle
        self._retention = retention
        self._validation = validation

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._state.connect() as conn:
            rows = conn.execute(
                """
                SELECT entry_id, run_id, operation, provider, model, lifecycle_state,
                       reserved_cost, actual_cost, refund_cost, reserved_input_tokens,
                       reserved_output_tokens, actual_input_tokens, actual_output_tokens,
                       actual_total_tokens, latency_ms, provider_usage_id, request_id,
                       price_version, cache_status, retry_attempt, created_at
                FROM model_call_entries
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self, days: int = 7) -> Dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat()
        with self._state.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(COALESCE(actual_total_tokens,
                           reserved_input_tokens + reserved_output_tokens)), 0) AS tokens,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency,
                       COALESCE(AVG(CASE WHEN lifecycle_state='settled' THEN 1.0 ELSE 0.0 END), 0)
                           AS success_rate
                FROM model_call_entries
                WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
        return {
            "period_days": max(0, int(days)),
            "calls": int(row["calls"] or 0),
            "total_tokens": int(row["tokens"] or 0),
            "avg_latency_ms": round(float(row["avg_latency"] or 0.0), 1),
            "success_rate": round(float(row["success_rate"] or 0.0), 3),
        }

    def run_summary(self, run_id: str) -> Dict[str, Any]:
        requested_run_id = str(run_id or "").strip()
        run_id = _canonical_run_id(run_id)
        with self._state.connect() as conn:
            run = conn.execute(
                "SELECT run_id, cost_budget, created_at FROM model_call_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None and _is_canonical_run_id(requested_run_id):
                candidate_run = conn.execute(
                    "SELECT run_id, cost_budget, created_at FROM model_call_runs WHERE run_id=?",
                    (requested_run_id,),
                ).fetchone()
                if candidate_run is not None:
                    run_id, run = requested_run_id, candidate_run
            if run is None and self._retention._run_tombstone_exists(conn, requested_run_id or run_id):
                # A previously issued key remains the caller's stable lookup
                # result after deletion without authorizing a new durable run.
                run_id = requested_run_id or run_id
            if run is None:
                return {"run_id": run_id, "exists": False}
            rows = conn.execute(
                "SELECT lifecycle_state, reserved_cost, actual_cost, refund_cost "
                "FROM model_call_entries WHERE run_id=?",
                (run_id,),
            ).fetchall()
            tombstoned_cost = self._retention._run_tombstoned_cost(conn, run_id)
        reserved = sum(float(row["reserved_cost"] or 0.0) for row in rows)
        actual = sum(
            float(row["actual_cost"] or 0.0)
            for row in rows
            if row["actual_cost"] is not None
        )
        refund = sum(float(row["refund_cost"] or 0.0) for row in rows)
        effective = sum(
            0.0
            if str(row["lifecycle_state"]) in {"released", "legacy_observed"}
            else (
                float(row["actual_cost"])
                if str(row["lifecycle_state"]) in {"settled", "incurred_overrun"}
                and row["actual_cost"] is not None
                else float(row["reserved_cost"] or 0.0)
            )
            for row in rows
        )
        states: Dict[str, int] = {}
        for row in rows:
            state = str(row["lifecycle_state"])
            states[state] = states.get(state, 0) + 1
        return {
            "run_id": run_id,
            "exists": True,
            "cost_budget": run["cost_budget"],
            "reserved_cost": reserved,
            "actual_cost": actual,
            "refund_cost": refund,
            # A settled provider receipt may exceed the conservative estimate.
            # Budget decisions must therefore use the same state-aware cost as
            # the reservation and daily-cap queries, never ``reserved-refund``.
            "effective_cost": effective + tombstoned_cost,
            "tombstoned_effective_cost": tombstoned_cost,
            "entry_count": len(rows),
            "states": states,
        }

    @staticmethod
    def inspect(config: Any | None = None) -> Dict[str, Any]:
        """Read ledger health without creating directories, files, or tables."""
        path = RuntimePaths.from_config(config).model_call_ledger_db
        state = LedgerState(path, config=config)
        validation = LedgerSchemaValidation(state)
        retention = LedgerSubjectsRetention(state)
        legacy_path_count, legacy_row_count = LedgerSubjectsRetention._retired_prompt_storage(config)
        try:
            configured_daily_cap_raw = _config_get(
                config, "model_call_ledger.daily_cost_cap", 50.0
            )
            if configured_daily_cap_raw is None:
                raise ModelCallLedgerInvariantError("daily model-call cost cap must be configured")
            configured_daily_cap = _nonnegative_finite_float(
                configured_daily_cap_raw,
                label="daily model-call cost cap",
            )
            invalid_daily_cost_cap = 0
        except ModelCallLedgerInvariantError:
            # Health must report malformed budget config as a blocker rather
            # than converting NaN into a comparison that silently passes.
            configured_daily_cap = 0.0
            invalid_daily_cost_cap = 1
        result: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            # Health is a public diagnostic surface.  Do not disclose an
            # absolute user filesystem path merely to identify this owner.
            "storage_owner": "model_call_ledger",
            "exists": path.is_file(),
            "status": "uninitialized",
            "model_call_storage_path_count": 1 + legacy_path_count,
            "health_ledger_path_mismatch": legacy_path_count,
            "legacy_prompt_storage_path_count": legacy_path_count,
            "legacy_prompt_call_row_count": legacy_row_count,
            "billable_calls_without_ledger": legacy_row_count,
            "billable_request_without_reservation": 0,
            "settled_cost_without_provider_usage": 0,
            "sensitive_prompt_preview": 0,
            "unverified_provider_usage": 0,
            "reservation_cost_overrun": 0,
            "subject_attribution_schema_missing": 0,
            "entry_subject_attribution_schema_missing": 0,
            "privacy_dispatch_schema_missing": 0,
            "metered_usage_receipt_schema_missing": 0,
            "unattributed_model_call_run_count": 0,
            "unattributed_billable_entry_count": 0,
            "frozen_subject_count": 0,
            "inflight_model_call_entry_count": 0,
            "stale_inflight_model_call_entry_count": 0,
            "daily_tombstoned_spend": 0.0,
            "run_tombstoned_spend": 0.0,
            "unrecoverable_run_tombstone_history_disposition": 0,
            "runtime_schema_gap_count": 0,
            "daily_effective_cost": 0.0,
            "daily_cost_cap": configured_daily_cap,
            "invalid_daily_cost_cap": invalid_daily_cost_cap,
        }
        if not path.is_file():
            if legacy_path_count:
                result.update(status="degraded", error="legacy_prompt_storage_not_reconciled")
            return result
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with _readonly_sqlite_connection(uri) as conn:
                conn.row_factory = sqlite3.Row
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if {"model_call_runs", "model_call_entries"} - tables:
                    result.update(status="degraded", error="model_call_ledger_schema_missing")
                    return result
                if "model_call_run_subjects" not in tables:
                    result["subject_attribution_schema_missing"] = 1
                    result["error"] = "subject_attribution_schema_missing"
                if "model_call_entry_subjects" not in tables:
                    result["entry_subject_attribution_schema_missing"] = 1
                    result["error"] = "entry_subject_attribution_schema_missing"
                if {
                    "model_call_frozen_subjects",
                    "model_call_daily_spend_tombstones",
                    "model_call_run_spend_tombstones",
                } - tables:
                    result["privacy_dispatch_schema_missing"] = 1
                    result["error"] = "privacy_dispatch_schema_missing"
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(model_call_entries)").fetchall()
                }
                result["sensitive_prompt_preview"] = len(columns & _FORBIDDEN_ENTRY_COLUMNS)
                if "metered_usage_receipt" not in columns:
                    result["metered_usage_receipt_schema_missing"] = 1
                    result["error"] = "metered_usage_receipt_schema_missing"
                result["billable_request_without_reservation"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM model_call_entries "
                        "WHERE lifecycle_state NOT IN ('legacy_observed') AND "
                        "(reserved_cost IS NULL OR (lifecycle_state IN "
                        "('settled', 'usage_unverified', 'incurred_unknown', 'incurred_overrun') "
                        "AND request_dispatched<>1))"
                    ).fetchone()[0]
                )
                if "metered_usage_receipt" in columns:
                    result["settled_cost_without_provider_usage"] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM model_call_entries "
                            "WHERE lifecycle_state='settled' "
                            "AND (metered_usage_receipt='' OR actual_cost IS NULL)"
                        ).fetchone()[0]
                    )
                else:
                    result["settled_cost_without_provider_usage"] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM model_call_entries "
                            "WHERE lifecycle_state='settled'"
                        ).fetchone()[0]
                    )
                result["unverified_provider_usage"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM model_call_entries "
                        "WHERE lifecycle_state IN ('usage_unverified', 'incurred_unknown')"
                    ).fetchone()[0]
                )
                result["reservation_cost_overrun"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM model_call_entries "
                        "WHERE lifecycle_state='incurred_overrun'"
                    ).fetchone()[0]
                    or 0
                )
                if "model_call_run_subjects" in tables:
                    result["unattributed_model_call_run_count"] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM model_call_runs r WHERE NOT EXISTS ("
                            "SELECT 1 FROM model_call_run_subjects s WHERE s.run_id=r.run_id)"
                        ).fetchone()[0]
                    )
                if "model_call_entry_subjects" in tables:
                    result["unattributed_billable_entry_count"] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM model_call_entries e WHERE NOT EXISTS ("
                            "SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id)"
                        ).fetchone()[0]
                    )
                if "model_call_frozen_subjects" in tables:
                    result["frozen_subject_count"] = int(
                        conn.execute("SELECT COUNT(*) FROM model_call_frozen_subjects").fetchone()[0]
                    )
                result["inflight_model_call_entry_count"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM model_call_entries "
                        "WHERE lifecycle_state='reserved' AND request_dispatched=1"
                    ).fetchone()[0]
                )
                result["stale_inflight_model_call_entry_count"] = retention._stale_inflight_entry_count(
                    conn
                )
                if "model_call_daily_spend_tombstones" in tables:
                    result["daily_tombstoned_spend"] = retention._daily_tombstoned_cost(
                        conn,
                        _utc_day(),
                    )
                if "model_call_run_spend_tombstones" in tables:
                    result["run_tombstoned_spend"] = float(
                        conn.execute(
                            "SELECT COALESCE(SUM(effective_cost), 0) "
                            "FROM model_call_run_spend_tombstones"
                        ).fetchone()[0]
                        or 0.0
                    )
                if "model_call_ledger_reconciliation_dispositions" in tables:
                    result["unrecoverable_run_tombstone_history_disposition"] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM model_call_ledger_reconciliation_dispositions "
                            "WHERE disposition_key='legacy_cascading_run_tombstone_history_v1' "
                            "AND disposition='explicit_unrecoverable_history_discard'"
                        ).fetchone()[0]
                        or 0
                    )
                runtime_gaps = validation._runtime_schema_gaps(conn)
                result["runtime_schema_gap_count"] = len(runtime_gaps)
                if runtime_gaps and "error" not in result:
                    result["error"] = "model_call_ledger_schema_invalid"
                result["daily_effective_cost"] = LedgerLifecycle._effective_cost(
                    conn,
                    "substr(created_at, 1, 10)=?",
                    (_utc_day(),),
                ) + float(result["daily_tombstoned_spend"])
                result["status"] = "ok"
                if any(
                    int(result[key]) > 0
                    for key in (
                        "billable_calls_without_ledger",
                        "billable_request_without_reservation",
                        "settled_cost_without_provider_usage",
                        "sensitive_prompt_preview",
                        "health_ledger_path_mismatch",
                        "unverified_provider_usage",
                        "reservation_cost_overrun",
                        "subject_attribution_schema_missing",
                        "entry_subject_attribution_schema_missing",
                        "privacy_dispatch_schema_missing",
                        "metered_usage_receipt_schema_missing",
                        "unattributed_model_call_run_count",
                        "unattributed_billable_entry_count",
                        "unrecoverable_run_tombstone_history_disposition",
                        "runtime_schema_gap_count",
                        "invalid_daily_cost_cap",
                        "stale_inflight_model_call_entry_count",
                    )
                ):
                    result["status"] = "degraded"
                cap = float(result["daily_cost_cap"])
                if not int(result["invalid_daily_cost_cap"]) and _money_exceeds(
                    float(result["daily_effective_cost"]), cap
                ):
                    result["status"] = "degraded"
                    result["daily_cap_exceeded"] = True
        except ModelCallLedgerInvariantError:
            result.update(status="degraded", error="model_call_ledger_data_invalid")
        except (sqlite3.Error, OSError, ValueError) as exc:
            result.update(status="error", error=type(exc).__name__)
        return result
