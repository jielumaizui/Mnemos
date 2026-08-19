"""Reservation, dispatch and settlement state machine implementation."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable

from core.db_utils import render_sql

from .contracts import (
    SCHEMA_VERSION,
    ModelCallBudgetExceeded,
    ModelCallLedgerInvariantError,
    ModelCallSubjectFrozen,
)
from .normalization import (
    MeteredProviderUsage,
    MeteredProviderUsageReceipt,
    _canonical_run_id,
    _config_get,
    _cost,
    _hash_text,
    _is_canonical_run_id,
    _is_prior_canonical_run_id,
    _money_equal,
    _money_exceeds,
    _new_canonical_entry_id,
    _nonnegative_finite_float,
    _nonnegative_int,
    _normalize_cache_status,
    _normalize_metered_usage_receipt,
    _normalize_model_label,
    _normalize_operation,
    _normalize_provider_label,
    _opaque_metadata_reference,
    _price_snapshot,
    _safe_error_code,
    _utc_day,
    _utc_now,
    _utf8_input_token_upper_bound,
    _issued_metered_usage_facts,
)
from .state import LedgerState
from .subjects_retention import LedgerSubjectsRetention


logger = logging.getLogger(__name__)


class LedgerLifecycle(LedgerSubjectsRetention):
    """Internal owner of the one-way provider-call lifecycle."""

    def __init__(self, state: LedgerState):
        super().__init__(state)

    @property
    def _config(self) -> Any | None:
        return self._state.config

    def start_run(
        self,
        run_id: str | None = None,
        *,
        cost_budget: float | None = None,
        subject_scope: tuple[str, str] | None = None,
    ) -> str:
        self._require_runtime_write_ready()
        requested_run_id = str(run_id or "").strip()
        run_id = _canonical_run_id(run_id)
        normalized_budget = (
            None
            if cost_budget is None
            else _nonnegative_finite_float(cost_budget, label="run cost budget")
        )
        subject_binding = self._subject_binding(subject_scope)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="run start")
            current = conn.execute(
                "SELECT cost_budget FROM model_call_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if current is None and _is_canonical_run_id(requested_run_id):
                persisted = conn.execute(
                    "SELECT cost_budget FROM model_call_runs WHERE run_id=?", (requested_run_id,)
                ).fetchone()
                if persisted is not None:
                    run_id, current = requested_run_id, persisted
                elif self._run_tombstone_exists(conn, requested_run_id):
                    raise ModelCallLedgerInvariantError(
                        "deleted model-call run ids are permanently retired and cannot be reused"
                    )
            elif current is None and _is_prior_canonical_run_id(requested_run_id):
                if self._run_tombstone_exists(conn, requested_run_id):
                    raise ModelCallLedgerInvariantError(
                        "deleted model-call run ids are permanently retired and cannot be reused"
                    )
            if current is None:
                if subject_binding is None:
                    raise ModelCallLedgerInvariantError(
                        "new model-call run requires an explicit subject scope"
                    )
                if self._run_tombstone_exists(conn, run_id):
                    raise ModelCallLedgerInvariantError(
                        "deleted model-call run ids are permanently retired and cannot be reused"
                    )
                self._assert_subjects_not_frozen(conn, (subject_binding,))
                now = _utc_now()
                conn.execute(
                    "INSERT INTO model_call_runs(run_id, cost_budget, created_at, schema_version) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, normalized_budget, now, SCHEMA_VERSION),
                )
                conn.execute(
                    "INSERT INTO model_call_run_subjects("
                    "run_id, scope_kind, subject_hash, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, subject_binding[0], subject_binding[1], now),
                )
            else:
                existing_binding = self._run_binding(conn, run_id)
                if subject_binding is not None and existing_binding != subject_binding:
                    raise ModelCallLedgerInvariantError(
                        "run_id was reused with a different subject attribution"
                    )
                self._assert_subjects_not_frozen(conn, (existing_binding,))
                if (
                    normalized_budget is not None
                    and current["cost_budget"] is not None
                    and not _money_equal(
                        _nonnegative_finite_float(
                            current["cost_budget"], label="persisted run cost budget"
                        ),
                        normalized_budget,
                    )
                ):
                    raise ModelCallLedgerInvariantError(
                        "run_id was reused with a different cost budget"
                    )
            conn.commit()
        return run_id

    def reserve(
        self,
        *,
        run_id: str | None,
        operation: str,
        provider: str,
        model: str,
        input_text: str,
        input_tokens: int,
        output_tokens: int = 0,
        cache_status: str = "miss",
        retry_attempt: int = 0,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> tuple[str, float]:
        """Atomically reserve worst-case configured cost before provider dispatch."""
        self._require_runtime_write_ready()
        normalized_operation = _normalize_operation(operation)
        # Pricing needs the provider-visible labels, but those labels are not
        # safe durable metadata: a provider default rate would otherwise let
        # any caller encode arbitrary text into the ledger's model column.
        # Snapshot the price first, then store only one-way label references.
        # Validate the caller-facing identifiers before price lookup.  An
        # arbitrary config/default match must not turn unbounded caller text
        # into a durable ledger field.  The durable labels below are then
        # domain-separated references even for short safe-looking inputs.
        provider_for_pricing = _normalize_provider_label(provider)
        model_for_pricing = _normalize_model_label(model)
        input_price, output_price, price_version = _price_snapshot(
            provider_for_pricing,
            model_for_pricing,
            self._config,
        )
        normalized_provider = _opaque_metadata_reference("provider_label", provider_for_pricing)
        normalized_model = _opaque_metadata_reference("model_label", model_for_pricing)
        normalized_cache_status = _normalize_cache_status(cache_status)
        if not str(run_id or "").strip():
            raise ModelCallLedgerInvariantError(
                "reservation requires a pre-created attributed model-call run"
            )
        run_id = self.start_run(run_id)
        reserved_input_tokens = _nonnegative_int(input_tokens, label="reservation input tokens")
        reserved_output_tokens = _nonnegative_int(output_tokens, label="reservation output tokens")
        input_token_upper_bound = _utf8_input_token_upper_bound(input_text)
        if reserved_input_tokens < input_token_upper_bound:
            raise ModelCallLedgerInvariantError(
                "reservation input tokens must cover the complete canonical provider payload"
            )
        normalized_retry_attempt = _nonnegative_int(
            retry_attempt, label="reservation retry attempt"
        )
        reserved_cost = _cost(
            reserved_input_tokens, reserved_output_tokens, input_price, output_price
        )
        entry_id = _new_canonical_entry_id()
        daily_cap_raw = _config_get(self._config, "model_call_ledger.daily_cost_cap", 50.0)
        if daily_cap_raw is None:
            raise ModelCallLedgerInvariantError("daily model-call cost cap must be configured")
        daily_cap = _nonnegative_finite_float(daily_cap_raw, label="daily model-call cost cap")

        recovered_stale_count = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="reservation")
            now = _utc_now()
            recovered_stale_count = self._recover_stale_dispatched_reservations(
                conn,
                settled_at=now,
            )
            unresolved_overrun = conn.execute(
                "SELECT 1 FROM model_call_entries "
                "WHERE lifecycle_state='incurred_overrun' LIMIT 1"
            ).fetchone()
            if unresolved_overrun is not None:
                raise ModelCallLedgerInvariantError(
                    "model-call ledger has an unresolved provider cost overrun"
                )
            run = conn.execute(
                "SELECT cost_budget FROM model_call_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ModelCallLedgerInvariantError("run disappeared before reservation")
            run_binding = self._run_binding(conn, run_id)
            entry_bindings = set(self._subject_bindings(subject_scopes))
            entry_bindings.add(run_binding)
            self._assert_subjects_not_frozen(conn, entry_bindings)
            run_effective = self._effective_cost(conn, "run_id=?", (run_id,)) + self._run_tombstoned_cost(
                conn, run_id
            )
            budget = (
                None
                if run["cost_budget"] is None
                else _nonnegative_finite_float(
                    run["cost_budget"], label="persisted run cost budget"
                )
            )
            if budget is not None and _money_exceeds(run_effective + reserved_cost, budget):
                raise ModelCallBudgetExceeded(
                    f"run budget would be exceeded: {run_effective + reserved_cost:.17g} > {float(budget):.17g}"
                )
            daily_effective = self._effective_cost(
                conn,
                "substr(created_at, 1, 10)=?",
                (_utc_day(),),
            ) + self._daily_tombstoned_cost(conn, _utc_day())
            if _money_exceeds(daily_effective + reserved_cost, daily_cap):
                raise ModelCallBudgetExceeded(
                    f"daily budget would be exceeded: {daily_effective + reserved_cost:.17g} > {daily_cap:.17g}"
                )
            conn.execute(
                """
                INSERT INTO model_call_entries(
                    entry_id, run_id, operation, provider, model, input_digest,
                    reserved_input_tokens, reserved_output_tokens, reserved_cost,
                    price_version, input_price, output_price, cache_status, retry_attempt,
                    lifecycle_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    entry_id,
                    run_id,
                    normalized_operation,
                    normalized_provider,
                    normalized_model,
                    _hash_text(input_text),
                    reserved_input_tokens,
                    reserved_output_tokens,
                    reserved_cost,
                    price_version,
                    input_price,
                    output_price,
                    normalized_cache_status,
                    normalized_retry_attempt,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO model_call_entry_subjects("
                "entry_id, scope_kind, subject_hash, created_at) VALUES (?, ?, ?, ?)",
                [
                    (entry_id, scope_kind, subject_hash, _utc_now())
                    for scope_kind, subject_hash in sorted(entry_bindings)
                ],
            )
            conn.execute("COMMIT")
        if recovered_stale_count:
            logger.warning(
                "Recovered stale dispatched model-call reservations count=%d",
                recovered_stale_count,
            )
        return entry_id, reserved_cost

    def _recover_stale_dispatched_reservations(
        self,
        conn: sqlite3.Connection,
        *,
        settled_at: str,
    ) -> int:
        """Conservatively close stale dispatches without refunding unknown spend."""
        entry_ids = self._stale_inflight_entry_ids(conn)
        if not entry_ids:
            return 0
        error_code = _safe_error_code(
            "stale_dispatched_reservation_recovered",
            default="provider_usage_unknown",
        )
        for entry_id in entry_ids:
            cursor = conn.execute(
                """
                UPDATE model_call_entries
                SET lifecycle_state='incurred_unknown', actual_cost=reserved_cost,
                    actual_total_tokens=reserved_input_tokens + reserved_output_tokens,
                    error_code=?, settled_at=?
                WHERE entry_id=?
                  AND lifecycle_state='reserved' AND request_dispatched=1
                """,
                (error_code, settled_at, entry_id),
            )
            if cursor.rowcount != 1:
                raise ModelCallLedgerInvariantError(
                    "stale dispatched reservation recovery was not atomic"
                )
        return len(entry_ids)

    @staticmethod
    def _effective_cost(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> float:
        row = conn.execute(
            render_sql(
                """
            SELECT COALESCE(SUM(
                CASE lifecycle_state
                    WHEN 'settled' THEN COALESCE(actual_cost, reserved_cost)
                    WHEN 'incurred_overrun' THEN COALESCE(actual_cost, reserved_cost)
                    WHEN 'released' THEN 0
                    WHEN 'legacy_observed' THEN 0
                    ELSE reserved_cost
                END
            ), 0) AS amount
            FROM model_call_entries
            WHERE {predicate}
            """,
                fixed_fragments={
                    "predicate": (
                        where,
                        {"run_id=?", "substr(created_at, 1, 10)=?"},
                    )
                },
            ),
            params,
        ).fetchone()
        return _nonnegative_finite_float(
            row["amount"] if row and row["amount"] is not None else 0.0,
            label="persisted effective model-call cost",
        )

    def _mark_dispatched(self, entry_id: str) -> None:
        self._require_runtime_write_ready()
        frozen = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="provider dispatch")
            bindings = [
                (str(row["scope_kind"]), str(row["subject_hash"]))
                for row in conn.execute(
                    "SELECT scope_kind, subject_hash FROM model_call_entry_subjects WHERE entry_id=?",
                    (entry_id,),
                ).fetchall()
            ]
            if not bindings:
                raise ModelCallLedgerInvariantError(
                    "reservation lacks immutable entry subject attribution"
                )
            try:
                self._assert_subjects_not_frozen(conn, bindings)
            except ModelCallSubjectFrozen:
                cursor = conn.execute(
                    """
                    UPDATE model_call_entries
                    SET lifecycle_state='released', refund_cost=reserved_cost,
                        error_code='subject_frozen_before_dispatch', settled_at=?
                    WHERE entry_id=? AND lifecycle_state='reserved' AND request_dispatched=0
                    """,
                    (_utc_now(), entry_id),
                )
                if cursor.rowcount != 1:
                    raise ModelCallLedgerInvariantError(
                        "frozen reservation was not available for release"
                    )
                frozen = True
            else:
                cursor = conn.execute(
                    """
                    UPDATE model_call_entries
                    SET request_dispatched=1, dispatched_at=?
                    WHERE entry_id=? AND lifecycle_state='reserved' AND request_dispatched=0
                    """,
                    (_utc_now(), entry_id),
                )
                if cursor.rowcount != 1:
                    raise ModelCallLedgerInvariantError("reservation was not available for dispatch")
            conn.commit()
        if frozen:
            raise ModelCallSubjectFrozen("model-call subject is frozen before provider dispatch")

    def _settle(
        self,
        entry_id: str,
        *,
        usage: MeteredProviderUsageReceipt,
        latency_ms: int,
    ) -> None:
        self._require_runtime_write_ready()
        if not isinstance(usage, MeteredProviderUsage) or not usage.is_factory_issued:
            raise ModelCallLedgerInvariantError("settlement requires a factory-issued metered receipt")
        (
            issued_input_tokens,
            issued_output_tokens,
            issued_meter_receipt,
            issued_provider_usage_id,
            issued_request_id,
        ) = _issued_metered_usage_facts(usage)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="provider settlement")
            row = conn.execute(
                "SELECT * FROM model_call_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if (
                row is None
                or row["lifecycle_state"] != "reserved"
                or not bool(row["request_dispatched"])
            ):
                raise ModelCallLedgerInvariantError("only a dispatched reservation can settle")
            input_value = _nonnegative_int(
                issued_input_tokens, label="provider metered input tokens"
            )
            output_value = _nonnegative_int(
                issued_output_tokens, label="provider metered output tokens"
            )
            total = input_value + output_value
            # A transport/request id proves acceptance, not provider metering.
            # ``metered_usage_receipt`` is only constructed by
            # ``metered_provider_usage`` after an explicit provider token meter.
            usage_id = _opaque_metadata_reference("provider_usage", issued_provider_usage_id)
            request_id = _opaque_metadata_reference(
                "request",
                issued_request_id or issued_provider_usage_id,
            )
            input_price = float(row["input_price"] or 0.0)
            output_price = float(row["output_price"] or 0.0)
            actual_cost = _cost(input_value, output_value, input_price, output_price)
            reservation_cost = _nonnegative_finite_float(
                row["reserved_cost"], label="persisted reserved cost"
            )
            exceeded_reservation = _money_exceeds(actual_cost, reservation_cost)
            refund = max(0.0, reservation_cost - actual_cost)
            cursor = conn.execute(
                """
                UPDATE model_call_entries
                SET actual_input_tokens=?, actual_output_tokens=?, actual_total_tokens=?, actual_cost=?,
                    refund_cost=?, latency_ms=?, provider_usage_id=?, metered_usage_receipt=?, request_id=?,
                    lifecycle_state=?, error_code=?, settled_at=?
                WHERE entry_id=? AND lifecycle_state='reserved' AND request_dispatched=1
                """,
                (
                    input_value,
                    output_value,
                    total,
                    actual_cost,
                    refund,
                    _nonnegative_int(latency_ms, label="provider latency"),
                    usage_id,
                    _normalize_metered_usage_receipt(issued_meter_receipt, preserve_canonical=True),
                    request_id,
                    "incurred_overrun" if exceeded_reservation else "settled",
                    "provider_actual_exceeds_reservation" if exceeded_reservation else "",
                    _utc_now(),
                    entry_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ModelCallLedgerInvariantError("settlement transition failed")
            conn.commit()

    def _release(self, entry_id: str, *, error_code: str) -> None:
        self._require_runtime_write_ready()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="pre-dispatch release")
            cursor = conn.execute(
                """
                UPDATE model_call_entries
                SET lifecycle_state='released', refund_cost=reserved_cost, error_code=?, settled_at=?
                WHERE entry_id=? AND lifecycle_state='reserved' AND request_dispatched=0
                """,
                (
                    _safe_error_code(error_code, default="pre_dispatch_failure"),
                    _utc_now(),
                    entry_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ModelCallLedgerInvariantError("only an undispatched reservation can release")
            conn.commit()

    def _preserve_incurred(self, entry_id: str, *, error_code: str) -> None:
        self._require_runtime_write_ready()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="incurred-cost preservation")
            cursor = conn.execute(
                """
                UPDATE model_call_entries
                SET lifecycle_state='incurred_unknown', actual_cost=reserved_cost,
                    actual_total_tokens=reserved_input_tokens + reserved_output_tokens,
                    error_code=?, settled_at=?
                WHERE entry_id=? AND lifecycle_state='reserved' AND request_dispatched=1
                """,
                (
                    _safe_error_code(error_code, default="provider_usage_unknown"),
                    _utc_now(),
                    entry_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ModelCallLedgerInvariantError("only a dispatched reservation can preserve incurred cost")
            conn.commit()
