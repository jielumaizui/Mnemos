"""The sole public seam for durable local model-call accounting.

Callers use this module through ``core.telemetry.prompt_call_log``.  The
facade deliberately owns no second persistence path: all state changes are
delegated to the internal schema, lifecycle, retention and reporting modules.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

from core.runtime_paths import RuntimePaths

from .contracts import (
    ModelCallBudgetExceeded,
    ModelCallLedgerError,
    ModelCallLedgerInvariantError,
    ModelCallRecord,
    ModelCallSubjectFrozen,
)
from .lifecycle import LedgerLifecycle
from .normalization import (
    MeteredProviderUsage,
    MeteredProviderUsageReceipt,
    _hash_text,
    has_metered_provider_usage,
    metered_provider_usage,
)
from .reporting import LedgerReporting
from .schema_reconciliation import LedgerSchemaReconciliation
from .schema_validation import LedgerSchemaValidation
from .state import LedgerState
from .subjects_retention import LedgerSubjectsRetention


_ACTIVE_MODEL_CALL_RUN: ContextVar[tuple[Any, str] | None] = ContextVar(
    "active_model_call_run",
    default=None,
)


class ModelCallReservation:
    """One pre-dispatch reservation with explicit terminal transitions."""

    def __init__(self, ledger: "ModelCallLedger", entry_id: str, reserved_cost: float):
        self._ledger = ledger
        self.entry_id = entry_id
        self.reserved_cost = float(reserved_cost)
        self._dispatched = False
        self._terminal = False

    @property
    def dispatched(self) -> bool:
        return self._dispatched

    def mark_dispatched(self) -> None:
        if self._terminal or self._dispatched:
            raise ModelCallLedgerInvariantError("cannot dispatch a terminal reservation")
        try:
            self._ledger._mark_dispatched(self.entry_id)
        except ModelCallSubjectFrozen:
            self._terminal = True
            raise
        self._dispatched = True

    def settle(self, *, usage: MeteredProviderUsageReceipt, latency_ms: int = 0) -> None:
        if self._terminal:
            raise ModelCallLedgerInvariantError("reservation already terminal")
        if not isinstance(usage, MeteredProviderUsage) or not usage.is_factory_issued:
            raise ModelCallLedgerInvariantError(
                "settlement requires a metered provider usage receipt"
            )
        self._ledger._settle(self.entry_id, usage=usage, latency_ms=latency_ms)
        self._terminal = True

    def release(self, *, error_code: str = "pre_dispatch_failure") -> None:
        if self._terminal:
            return
        self._ledger._release(self.entry_id, error_code=error_code)
        self._terminal = True

    def preserve_incurred(self, *, error_code: str = "provider_usage_unknown") -> None:
        if self._terminal:
            return
        self._ledger._preserve_incurred(self.entry_id, error_code=error_code)
        self._terminal = True


class ModelCallLedger:
    """Durable owner of model-call reservation and settlement evidence."""

    def __init__(self, db_path: Path, *, config: Any | None = None, initialize: bool = True):
        self.db_path = Path(db_path).expanduser()
        self._config = config
        self._state = LedgerState(self.db_path, config=config)
        self._validation = LedgerSchemaValidation(self._state)
        self._schema_mutation = LedgerSchemaReconciliation(self._state)
        self._retention = LedgerSubjectsRetention(self._state)
        self._lifecycle = LedgerLifecycle(self._state)
        self._reporting = LedgerReporting(
            self._state,
            lifecycle=self._lifecycle,
            retention=self._retention,
            validation=self._validation,
        )
        if initialize:
            existed = self.db_path.exists()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if existed:
                self._validation._validate_runtime_schema()
            else:
                self._schema_mutation._bootstrap_schema()
                self._validation._validate_runtime_schema()
            self._state.runtime_schema_validated = True
        elif self.db_path.exists():
            self._validation._validate_runtime_schema()
            self._state.runtime_schema_validated = True
        self._runtime_schema_validated = self._state.runtime_schema_validated
        self._reconciliation_only = self._state.reconciliation_only

    @classmethod
    def for_config(
        cls, config: Any | None = None, *, initialize: bool = True
    ) -> "ModelCallLedger":
        return cls(
            RuntimePaths.from_config(config).model_call_ledger_db,
            config=config,
            initialize=initialize,
        )

    @staticmethod
    def path_for_config(config: Any | None = None) -> Path:
        return RuntimePaths.from_config(config).model_call_ledger_db

    def start_run(
        self,
        run_id: str | None = None,
        *,
        cost_budget: float | None = None,
        subject_scope: tuple[str, str] | None = None,
    ) -> str:
        return self._lifecycle.start_run(
            run_id,
            cost_budget=cost_budget,
            subject_scope=subject_scope,
        )

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
    ) -> ModelCallReservation:
        entry_id, reserved_cost = self._lifecycle.reserve(
            run_id=run_id,
            operation=operation,
            provider=provider,
            model=model,
            input_text=input_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_status=cache_status,
            retry_attempt=retry_attempt,
            subject_scopes=subject_scopes,
        )
        return ModelCallReservation(self, entry_id, reserved_cost)

    def _mark_dispatched(self, entry_id: str) -> None:
        self._lifecycle._mark_dispatched(entry_id)

    def _settle(
        self, entry_id: str, *, usage: MeteredProviderUsageReceipt, latency_ms: int
    ) -> None:
        self._lifecycle._settle(entry_id, usage=usage, latency_ms=latency_ms)

    def _release(self, entry_id: str, *, error_code: str) -> None:
        self._lifecycle._release(entry_id, error_code=error_code)

    def _preserve_incurred(self, entry_id: str, *, error_code: str) -> None:
        self._lifecycle._preserve_incurred(entry_id, error_code=error_code)

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._reporting.recent(limit)

    def stats(self, days: int = 7) -> Dict[str, Any]:
        return self._reporting.stats(days)

    def run_summary(self, run_id: str) -> Dict[str, Any]:
        return self._reporting.run_summary(run_id)

    def freeze_subject_scope(self, scope_kind: str, scope_value: str) -> Dict[str, Any]:
        return self._retention.freeze_subject_scope(scope_kind, scope_value)

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        return self._retention.cleanup_older_than(days, dry_run=dry_run)

    def delete_subject_scope(
        self, scope_kind: str, scope_value: str, *, dry_run: bool = False
    ) -> Dict[str, Any]:
        return self._retention.delete_subject_scope(
            scope_kind, scope_value, dry_run=dry_run
        )

    @classmethod
    def inspect(cls, config: Any | None = None) -> Dict[str, Any]:
        return LedgerReporting.inspect(config)


def current_model_call_run() -> tuple[ModelCallLedger, str] | None:
    """Return the request-scoped ledger run for a nested provider boundary."""
    return _ACTIVE_MODEL_CALL_RUN.get()


@contextmanager
def model_call_run_scope(
    config: Any,
    operation: str,
    *,
    cost_budget: float | None = None,
    subject_scope: tuple[str, str] | None = None,
) -> Iterator[tuple[ModelCallLedger, str]]:
    """Bind nested provider calls to one attributable durable run."""
    inherited = current_model_call_run()
    if inherited is not None:
        if subject_scope is not None:
            inherited[0].start_run(inherited[1], subject_scope=subject_scope)
        yield inherited
        return
    ledger = ModelCallLedger.for_config(config)
    run_id = ledger.start_run(
        f"model-call:{_hash_text(str(operation or 'model-call'))[:24]}:{uuid.uuid4().hex}",
        cost_budget=cost_budget,
        subject_scope=subject_scope,
    )
    token = _ACTIVE_MODEL_CALL_RUN.set((ledger, run_id))
    try:
        yield ledger, run_id
    finally:
        _ACTIVE_MODEL_CALL_RUN.reset(token)


class PromptCallLog:
    """Retired post-call writer; it intentionally never has a fallback path."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise ModelCallLedgerInvariantError(
            "PromptCallLog is retired; provider callers must use "
            "ModelCallLedger reserve/dispatch/settle"
        )


__all__ = [
    "ModelCallLedger",
    "ModelCallReservation",
    "ModelCallRecord",
    "ModelCallLedgerError",
    "ModelCallBudgetExceeded",
    "ModelCallLedgerInvariantError",
    "ModelCallSubjectFrozen",
    "MeteredProviderUsage",
    "metered_provider_usage",
    "has_metered_provider_usage",
    "current_model_call_run",
    "model_call_run_scope",
    "PromptCallLog",
]
